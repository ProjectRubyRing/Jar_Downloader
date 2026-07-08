#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# jar_manager.py
#   Python 補助実装 (標準エンジン)。jar_manager.sh から --engine python /
#   --engine java 指定時に呼び出される。単体でも実行可能。
#
#   対象: EC2 上の RHEL9 / python3 (3.6+ を想定。標準ライブラリのみ使用)
#
#   モード: download / export / validate  (+ --dry-run)
# ============================================================================
from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------------

# リストファイルのカラム順 (TSV ヘッダと一致させる)
COLUMNS = ["groupId", "artifactId", "version", "packaging",
           "classifier", "targetDir", "fileName"]

# デフォルトリポジトリ (優先順)。安全性の低い任意サイトは含めない。
DEFAULT_REPOS: List[str] = [
    "https://repo1.maven.org/maven2",                                  # Maven Central
    "https://repository.apache.org/content/repositories/releases",     # Apache
    "https://repo.spring.io/release",                                  # Spring
    "https://repository.jboss.org/nexus/content/repositories/releases",  # JBoss
    "https://oss.sonatype.org/content/repositories/releases",          # Sonatype Releases
]

# export 時に version の後ろに来ても「バージョンの一部」とみなす修飾子。
# (これらはハイフンで連結されていてもクラシファイアではなくバージョン扱い)
VERSION_QUALIFIERS = {
    "SNAPSHOT", "RELEASE", "FINAL", "GA", "SP1", "SP2", "SP3",
}

# export 時に「クラシファイア」とみなす代表的な語。
KNOWN_CLASSIFIERS = {
    "sources", "javadoc", "tests", "test-sources",
    "jar-with-dependencies", "shaded", "linux-x86_64",
    "native", "no_aop", "all",
}

# バージョン抽出用の正規表現。
#   ファイル名は "artifactId-version[-classifier].jar" を想定。
#   artifactId 自体にハイフンを含む (log4j-core, spring-core, jackson-databind)
#   ため「最初にハイフンの直後へ数字が来る位置」をバージョン開始とみなす。
#
#   限界・注意:
#     * artifactId が数字で終わり直後にハイフン数字が続く異常系
#       (例: commons-io2-1.0.jar) は誤判定の可能性がある。
#     * 日付バージョン (20231129) や 1.0.0.Final, 1.2.3.RELEASE も対象。
#     * 判別不能時は UNKNOWN を返し validate で検知できるようにする。
_VER_START_RE = re.compile(r"-(?=\d)")  # ハイフンの直後が数字


# ----------------------------------------------------------------------------
# データ構造
# ----------------------------------------------------------------------------
@dataclass
class Entry:
    """リストファイル1行 = 1 JAR を表す。"""
    groupId: str = ""
    artifactId: str = ""
    version: str = ""
    packaging: str = "jar"
    classifier: str = ""
    targetDir: str = ""
    fileName: str = ""
    lineno: int = 0  # 元ファイル上の行番号 (エラー表示用)

    def resolved_target_dir(self, default_target: Optional[str]) -> str:
        """targetDir が空なら --target-dir を用いる。"""
        return self.targetDir if self.targetDir else (default_target or "")

    def resolved_filename(self) -> str:
        """fileName が空なら Maven 標準形式で自動生成する。"""
        if self.fileName:
            return self.fileName
        ext = self.packaging or "jar"
        if self.classifier:
            return f"{self.artifactId}-{self.version}-{self.classifier}.{ext}"
        return f"{self.artifactId}-{self.version}.{ext}"


class JarManagerError(Exception):
    """業務的なエラー (終了コード != 0 に変換)。"""


# ----------------------------------------------------------------------------
# ロギング (標準出力 + ログファイル両対応)
# ----------------------------------------------------------------------------
def setup_logging(log_file: Optional[str]) -> logging.Logger:
    logger = logging.getLogger("jar_manager")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as exc:  # 権限エラー等
            logger.warning("ログファイルを開けません (%s): %s", log_file, exc)
    return logger


# ----------------------------------------------------------------------------
# セキュリティ / パス検証
# ----------------------------------------------------------------------------
def validate_safe_filename(name: str) -> None:
    """fileName にパス区切りや .. が含まれないこと (path traversal 防止)。"""
    if not name:
        raise JarManagerError("fileName が空です")
    if "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise JarManagerError(f"不正な fileName: {name!r}")


def validate_target_dir(target: str, allow_dirs: List[str],
                        logger: logging.Logger) -> str:
    """targetDir を検証し正規化した絶対パスを返す。

    * .. を含む相対的な上位参照は正規化後にチェック。
    * --allow-dir が指定されている場合、その配下でなければエラー。
      未指定の場合は警告のみ (実運用では --allow-dir 指定を推奨)。
    """
    if not target:
        raise JarManagerError("targetDir が未指定です (--target-dir も未指定)")
    norm = os.path.abspath(os.path.normpath(target))
    if allow_dirs:
        ok = any(norm == a or norm.startswith(a + os.sep)
                 for a in (os.path.abspath(x) for x in allow_dirs))
        if not ok:
            raise JarManagerError(
                f"targetDir が許可された配置先の外です: {norm} "
                f"(--allow-dir: {allow_dirs})")
    else:
        if os.path.isabs(target) is False and (".." in target.split(os.sep)):
            logger.warning("相対パスに '..' が含まれます: %s -> %s", target, norm)
    return norm


# ----------------------------------------------------------------------------
# リストファイル入出力
# ----------------------------------------------------------------------------
def read_list(path: str, logger: logging.Logger) -> List[Entry]:
    """TSV/CSV のリストファイルを読み込む。# コメント行・空行を無視。

    区切りは TAB を第一候補とし、TAB が無くカンマがある行は CSV とみなす。
    """
    if not os.path.isfile(path):
        raise JarManagerError(f"リストファイルが存在しません: {path}")

    entries: List[Entry] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                continue
            delim = "\t" if "\t" in line else ","
            fields = next(csv.reader([line], delimiter=delim))
            fields = [f.strip() for f in fields]
            # 足りないカラムは空文字で補完
            while len(fields) < len(COLUMNS):
                fields.append("")
            data = dict(zip(COLUMNS, fields[:len(COLUMNS)]))
            e = Entry(
                groupId=data["groupId"],
                artifactId=data["artifactId"],
                version=data["version"],
                packaging=data["packaging"] or "jar",
                classifier=data["classifier"],
                targetDir=data["targetDir"],
                fileName=data["fileName"],
                lineno=lineno,
            )
            entries.append(e)
    logger.debug("リスト読み込み: %d 件", len(entries))
    return entries


def write_list(path: str, entries: List[Entry], logger: logging.Logger,
               scan_dir: Optional[str] = None) -> None:
    """再取り込み可能な TSV リストを生成する。ヘッダはコメント行。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: List[str] = []
    lines.append("# ===========================================================")
    lines.append("# jar_manager export list (再取り込み可能な TSV 形式)")
    lines.append(f"# generated : {now}")
    if scan_dir:
        lines.append(f"# scan-dir  : {scan_dir}")
    lines.append("# columns   : " + "\t".join(COLUMNS))
    lines.append("# classifier が無い場合は空欄。fileName 空欄で Maven 標準名を自動生成。")
    lines.append("# groupId が UNKNOWN の行は座標未確定。validate 前に手動補正を推奨。")
    lines.append("# ===========================================================")
    # ヘッダ行 (コメント) : そのまま入力に使えるよう # 始まり
    lines.append("# " + "\t".join(COLUMNS))
    for e in entries:
        row = [e.groupId, e.artifactId, e.version, e.packaging or "jar",
               e.classifier, e.targetDir, e.fileName]
        lines.append("\t".join(row))

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)  # atomic
    logger.info("エクスポート完了: %s (%d 件)", path, len(entries))


# ----------------------------------------------------------------------------
# バージョン / artifactId 判定
# ----------------------------------------------------------------------------
def parse_jar_filename(filename: str) -> Tuple[str, str, str]:
    """ファイル名から (artifactId, version, classifier) を推定する。

    判別不能な場合、version には "UNKNOWN" を返す。
    """
    base = os.path.basename(filename)
    stem = base[:-4] if base.lower().endswith(".jar") else base

    # ハイフン直後に数字が来る最初の位置をバージョン開始とみなす
    m = _VER_START_RE.search(stem)
    if not m:
        return (stem, "UNKNOWN", "")
    idx = m.start()
    artifact = stem[:idx]
    rest = stem[idx + 1:]  # 例: 2.17.1 / 3.0.0-SNAPSHOT / 2.15.3-sources

    # rest を version と classifier に分割
    parts = rest.split("-")
    version = parts[0]
    classifier_tokens: List[str] = []
    for tok in parts[1:]:
        up = tok.upper()
        if up in VERSION_QUALIFIERS or tok in VERSION_QUALIFIERS:
            version += "-" + tok  # バージョンの一部
        elif tok in KNOWN_CLASSIFIERS or tok.lower() in KNOWN_CLASSIFIERS:
            classifier_tokens.append(tok)
        else:
            # 不明トークン: 数字始まりならバージョンの一部、そうでなければ classifier
            if tok[:1].isdigit():
                version += "-" + tok
            else:
                classifier_tokens.append(tok)
    classifier = "-".join(classifier_tokens)
    return (artifact, version, classifier)


def read_pom_properties(jar_path: str) -> Optional[Dict[str, str]]:
    """JAR 内 META-INF/maven/**/pom.properties から座標を抽出する。"""
    try:
        with zipfile.ZipFile(jar_path) as zf:
            candidates = [n for n in zf.namelist()
                          if n.startswith("META-INF/maven/")
                          and n.endswith("pom.properties")]
            if not candidates:
                return None
            with zf.open(candidates[0]) as fp:
                props: Dict[str, str] = {}
                for line in io.TextIOWrapper(fp, encoding="utf-8"):
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
                if props.get("groupId") and props.get("artifactId"):
                    return props
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return None


# ----------------------------------------------------------------------------
# JAR 妥当性検査 (Java 不要 / zipfile)
# ----------------------------------------------------------------------------
def verify_jar(path: str) -> bool:
    """ダウンロードした JAR が妥当な zip か検査する。"""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            return bad is None
    except (zipfile.BadZipFile, OSError):
        return False


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def _open(url: str, method: str, timeout: int) -> urllib.request.addinfourl:
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "jar_manager/1.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def http_exists(url: str, timeout: int, logger: logging.Logger) -> bool:
    """HEAD で存在確認。405 等で不可なら GET(Range) にフォールバック。"""
    try:
        with _open(url, "HEAD", timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405, 400):
            try:
                req = urllib.request.Request(
                    url, method="GET",
                    headers={"User-Agent": "jar_manager/1.0",
                             "Range": "bytes=0-0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return 200 <= resp.status < 400
            except urllib.error.URLError:
                return False
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.debug("HEAD 失敗 %s: %s", url, exc)
        return False


def http_download(url: str, dest_tmp: str, timeout: int,
                  logger: logging.Logger) -> None:
    """URL を一時ファイルへ保存する。失敗時は例外。"""
    with _open(url, "GET", timeout) as resp:
        if not (200 <= resp.status < 300):
            raise JarManagerError(f"HTTP {resp.status}: {url}")
        with open(dest_tmp, "wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 256)


def http_fetch_text(url: str, timeout: int) -> Optional[str]:
    """チェックサム等の小さなテキストを取得。失敗は None。"""
    try:
        with _open(url, "GET", timeout) as resp:
            if 200 <= resp.status < 300:
                return resp.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError):
        return None
    return None


def compute_hash(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# URL 解決
# ----------------------------------------------------------------------------
def build_url(repo_base: str, e: Entry) -> str:
    group_path = e.groupId.replace(".", "/")
    fname = e.resolved_filename()
    return f"{repo_base.rstrip('/')}/{group_path}/{e.artifactId}/{e.version}/{fname}"


# ----------------------------------------------------------------------------
# old 退避
# ----------------------------------------------------------------------------
def find_existing_same_artifact(target_dir: str, e: Entry) -> List[str]:
    """target_dir 直下で「バージョン以外は同名」の既存 JAR を探す。

    artifactId 基準で判定 (classifier も一致条件に含める)。
    退避対象となる既存ファイル名のリストを返す (新規と同名の完全一致は除外)。
    """
    if not os.path.isdir(target_dir):
        return []
    new_name = e.resolved_filename()
    result: List[str] = []
    for name in os.listdir(target_dir):
        if not name.lower().endswith(".jar"):
            continue
        full = os.path.join(target_dir, name)
        if not os.path.isfile(full):
            continue
        if name == new_name:
            continue  # 完全一致は退避不要 (冪等性)
        art, _ver, cls = parse_jar_filename(name)
        if art == e.artifactId and cls == e.classifier:
            result.append(name)
    return result


def evacuate_to_old(target_dir: str, filename: str, artifactId: str,
                    dry_run: bool, logger: logging.Logger) -> str:
    """既存 JAR を old/<artifactId>/<version>/ へ移動する。

    構成: target_dir/old/<バージョン除去名(=artifactId)>/<既存version>/<file>
    衝突時はタイムスタンプ付与で回避。移動先パスを返す。
    """
    _art, ver, _cls = parse_jar_filename(filename)
    ver_dir = ver if ver else "UNKNOWN"
    old_dir = os.path.join(target_dir, "old", artifactId, ver_dir)
    src = os.path.join(target_dir, filename)
    dst = os.path.join(old_dir, filename)

    if dry_run:
        logger.info("[dry-run] 退避予定: %s -> %s", src, dst)
        return dst

    os.makedirs(old_dir, exist_ok=True)
    if os.path.exists(dst):
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        root, ext = os.path.splitext(filename)
        dst = os.path.join(old_dir, f"{root}.{ts}{ext}")
        logger.warning("退避先が既存のためタイムスタンプ付与: %s", dst)
    shutil.move(src, dst)
    logger.info("退避: %s -> %s", src, dst)
    return dst


# ----------------------------------------------------------------------------
# download モード
# ----------------------------------------------------------------------------
def do_download(e: Entry, default_target: Optional[str], repos: List[str],
                timeout: int, retry: int, checksum_mode: str,
                allow_dirs: List[str], dry_run: bool,
                logger: logging.Logger) -> bool:
    """1 エントリをダウンロード・配置する。成功 True。"""
    # 必須項目
    for key in ("groupId", "artifactId", "version"):
        if not getattr(e, key):
            raise JarManagerError(f"必須項目 {key} が空 (行 {e.lineno})")

    target_dir = validate_target_dir(
        e.resolved_target_dir(default_target), allow_dirs, logger)
    fname = e.resolved_filename()
    validate_safe_filename(fname)
    dest = os.path.join(target_dir, fname)

    # 冪等性: 既に目的物が存在
    if os.path.isfile(dest):
        if checksum_mode == "skip":
            logger.info("既存のためスキップ: %s", dest)
            return True
        # 検証: 破損していれば再取得へ
        if verify_jar(dest):
            logger.info("既存 (妥当) のためスキップ: %s", dest)
            return True
        logger.warning("既存ファイルが破損。再取得します: %s", dest)

    # URL 解決 (リポジトリを順に試行)
    found_url: Optional[str] = None
    for repo in repos:
        url = build_url(repo, e)
        if dry_run:
            logger.info("[dry-run] 存在確認予定: %s", url)
            found_url = url
            break
        if http_exists(url, timeout, logger):
            found_url = url
            logger.debug("発見: %s", url)
            break
        logger.debug("未検出: %s", url)

    if not found_url:
        raise JarManagerError(
            f"全リポジトリで未検出: {e.groupId}:{e.artifactId}:{e.version}")

    if dry_run:
        logger.info("[dry-run] 作成予定 dir : %s", target_dir)
        existing = find_existing_same_artifact(target_dir, e)
        for old in existing:
            evacuate_to_old(target_dir, old, e.artifactId, True, logger)
        logger.info("[dry-run] DL 予定 URL  : %s", found_url)
        logger.info("[dry-run] 配置予定 path: %s", dest)
        return True

    # 配置先作成 (権限エラー考慮)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        raise JarManagerError(f"配置先を作成できません {target_dir}: {exc}")

    # 一時ファイルへダウンロード (リトライ付き)
    fd, tmp = tempfile.mkstemp(prefix=".jm_", suffix=".part", dir=target_dir)
    os.close(fd)
    try:
        last_err: Optional[Exception] = None
        for attempt in range(1, retry + 1):
            try:
                http_download(found_url, tmp, timeout, logger)
                break
            except (urllib.error.URLError, OSError, JarManagerError) as exc:
                last_err = exc
                logger.warning("DL 失敗 (%d/%d) %s: %s",
                               attempt, retry, found_url, exc)
        else:
            raise JarManagerError(f"ダウンロード失敗: {found_url} ({last_err})")

        # zip 妥当性
        if not verify_jar(tmp):
            raise JarManagerError(f"JAR が壊れています: {found_url}")

        # チェックサム検証
        if checksum_mode != "skip":
            ok = verify_checksum(found_url, tmp, timeout, logger)
            if ok is False and checksum_mode == "strict":
                raise JarManagerError(f"チェックサム不一致 (strict): {found_url}")
            elif ok is None and checksum_mode == "strict":
                raise JarManagerError(
                    f"チェックサム取得不可 (strict): {found_url}")

        # 新規 DL 成功 -> 既存を退避 (この順序でロールバック安全性を確保)
        for old in find_existing_same_artifact(target_dir, e):
            evacuate_to_old(target_dir, old, e.artifactId, False, logger)

        # atomic 配置
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        os.replace(tmp, dest)
        tmp = ""  # 移動済み
        logger.info("配置完了: %s", dest)
        return True
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)  # 中途半端な .part を残さない
            except OSError:
                pass


def verify_checksum(url: str, path: str, timeout: int,
                    logger: logging.Logger) -> Optional[bool]:
    """.sha256 -> .sha1 の順で取得し検証。取得不可なら None。"""
    for algo, ext in (("sha256", ".sha256"), ("sha1", ".sha1")):
        remote = http_fetch_text(url + ext, timeout)
        if not remote:
            continue
        remote_hash = remote.split()[0].lower()
        local_hash = compute_hash(path, algo)
        if local_hash == remote_hash:
            logger.debug("%s 一致: %s", algo, url)
            return True
        logger.warning("%s 不一致 %s: local=%s remote=%s",
                       algo, url, local_hash, remote_hash)
        return False
    logger.warning("チェックサム取得不可: %s", url)
    return None


# ----------------------------------------------------------------------------
# export モード
# ----------------------------------------------------------------------------
def do_export(scan_dir: str, output: str, include_old: bool,
              default_target: Optional[str],
              logger: logging.Logger) -> int:
    if not os.path.isdir(scan_dir):
        raise JarManagerError(f"scan-dir が存在しません: {scan_dir}")

    entries: List[Entry] = []
    for root, dirs, files in os.walk(scan_dir):
        # old 配下を除外 (デフォルト)
        if not include_old:
            rel = os.path.relpath(root, scan_dir).replace("\\", "/")
            if rel == "old" or rel.startswith("old/") or "/old/" in ("/" + rel + "/"):
                continue
            if "old" in dirs and not include_old:
                dirs[:] = [d for d in dirs if d != "old"]
        for name in files:
            if not name.lower().endswith(".jar"):
                continue
            full = os.path.join(root, name)
            e = build_entry_from_jar(full, name, default_target)
            entries.append(e)

    entries.sort(key=lambda x: (x.groupId, x.artifactId, x.version))
    write_list(output, entries, logger, scan_dir=scan_dir)
    unknown = sum(1 for e in entries if e.groupId == "UNKNOWN")
    if unknown:
        logger.warning("groupId 未確定が %d 件あります (UNKNOWN)", unknown)
    return 0


def build_entry_from_jar(full_path: str, name: str,
                         default_target: Optional[str]) -> Entry:
    """JAR から Entry を構築。pom.properties 優先、無ければファイル名推定。"""
    art_f, ver_f, cls_f = parse_jar_filename(name)
    props = read_pom_properties(full_path)
    if props:
        group = props.get("groupId", "UNKNOWN")
        artifact = props.get("artifactId", art_f)
        version = props.get("version", ver_f)
    else:
        group = "UNKNOWN"
        artifact = art_f
        version = ver_f
    target = os.path.dirname(full_path)
    return Entry(
        groupId=group,
        artifactId=artifact,
        version=version,
        packaging="jar",
        classifier=cls_f,
        targetDir=target,
        fileName=name,
    )


# ----------------------------------------------------------------------------
# validate モード
# ----------------------------------------------------------------------------
_VERSION_OK_RE = re.compile(r"^\d[\w.\-]*$")


def do_validate(entries: List[Entry], default_target: Optional[str],
                logger: logging.Logger) -> int:
    errors = 0
    warnings = 0
    for e in entries:
        prefix = f"行 {e.lineno}"
        if not e.artifactId:
            logger.error("%s: artifactId が空", prefix); errors += 1
        if not e.version:
            logger.error("%s: version が空", prefix); errors += 1
        elif not _VERSION_OK_RE.match(e.version):
            logger.error("%s: version 形式が不正: %r", prefix, e.version)
            errors += 1
        if not e.groupId:
            logger.error("%s: groupId が空", prefix); errors += 1
        elif e.groupId == "UNKNOWN":
            logger.warning("%s: groupId が UNKNOWN", prefix); warnings += 1
        if not e.resolved_target_dir(default_target):
            logger.error("%s: targetDir 未指定 (--target-dir も無し)", prefix)
            errors += 1
        try:
            validate_safe_filename(e.resolved_filename())
        except JarManagerError as exc:
            logger.error("%s: %s", prefix, exc); errors += 1
        if e.packaging and e.packaging != "jar":
            logger.warning("%s: packaging が jar 以外: %s", prefix, e.packaging)
            warnings += 1
    logger.info("validate 完了: %d 件, エラー %d, 警告 %d",
                len(entries), errors, warnings)
    return 1 if errors else 0


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="jar_manager.py",
        description="Maven 座標に基づく JAR ダウンロード / 退避 / 一覧生成ツール")
    p.add_argument("--mode", required=True,
                   choices=["download", "export", "validate", "report"])
    p.add_argument("--list", dest="list_file")
    p.add_argument("--target-dir")
    p.add_argument("--scan-dir")
    p.add_argument("--output")
    p.add_argument("--repo", action="append", default=None,
                   help="リポジトリ base URL (複数指定可、優先順)")
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--checksum-mode", choices=["warn", "strict", "skip"],
                   default="warn")
    p.add_argument("--allow-dir", action="append", default=[],
                   help="許可する配置先 (path traversal 対策、複数可)")
    p.add_argument("--include-old", action="store_true",
                   help="export / report で old 配下も対象にする")
    p.add_argument("--online", action="store_true",
                   help="report で Maven Central 照合により座標・概要を補完し "
                        "取得 URL を実在確認する")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-file")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logger = setup_logging(args.log_file)
    repos = args.repo if args.repo else DEFAULT_REPOS

    try:
        if args.mode == "validate":
            if not args.list_file:
                raise JarManagerError("--list が必要です")
            entries = read_list(args.list_file, logger)
            return do_validate(entries, args.target_dir, logger)

        if args.mode == "export":
            if not args.scan_dir or not args.output:
                raise JarManagerError("--scan-dir と --output が必要です")
            return do_export(args.scan_dir, args.output, args.include_old,
                             args.target_dir, logger)

        if args.mode == "report":
            if not args.scan_dir or not args.output:
                raise JarManagerError("--scan-dir と --output が必要です")
            try:
                import jar_report
            except ImportError as exc:
                raise JarManagerError(
                    f"jar_report モジュールを読み込めません: {exc}")
            try:
                return jar_report.generate(
                    args.scan_dir, args.output, args.online, repos,
                    args.timeout, args.include_old, logger)
            except (ValueError, RuntimeError) as exc:
                raise JarManagerError(str(exc))

        if args.mode == "download":
            if not args.list_file:
                raise JarManagerError("--list が必要です")
            entries = read_list(args.list_file, logger)
            failures = 0
            for e in entries:
                try:
                    do_download(e, args.target_dir, repos, args.timeout,
                                args.retry, args.checksum_mode,
                                args.allow_dir, args.dry_run, logger)
                except JarManagerError as exc:
                    logger.error("行 %d 失敗: %s", e.lineno, exc)
                    failures += 1
            if failures:
                logger.error("download 完了: %d 件失敗", failures)
                return 2
            logger.info("download 完了: 全 %d 件成功", len(entries))
            return 0
    except JarManagerError as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("中断されました")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
