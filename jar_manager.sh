#!/usr/bin/env bash
# ============================================================================
# jar_manager.sh
#   メイン実行スクリプト。--engine で実装方式を切り替える。
#     python : jar_manager.py を呼び出す標準実装
#     shell  : このスクリプト内で完結する Bash 実装
#     java   : Python 実装をベースに、export のメタデータ抽出で Java を利用
#
#   対象: EC2 上の RHEL9 / bash / python3 / curl or wget
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="${SCRIPT_DIR}/jar_manager.py"
JAVA_SRC="${SCRIPT_DIR}/java/JarMetaReader.java"

# ---- デフォルト値 ----------------------------------------------------------
MODE=""
LIST_FILE=""
TARGET_DIR=""
SCAN_DIR=""
OUTPUT=""
ENGINE="python"
DRY_RUN="false"
TIMEOUT="30"
RETRY="3"
CHECKSUM_MODE="warn"
LOG_FILE=""
INCLUDE_OLD="false"
ONLINE="false"
declare -a REPOS=()
declare -a ALLOW_DIRS=()

# デフォルトリポジトリ (優先順)。安全性の低い任意サイトは含めない。
declare -a DEFAULT_REPOS=(
  "https://repo1.maven.org/maven2"
  "https://repository.apache.org/content/repositories/releases"
  "https://repo.spring.io/release"
  "https://repository.jboss.org/nexus/content/repositories/releases"
  "https://oss.sonatype.org/content/repositories/releases"
)

# export 時にバージョンの一部とみなす修飾子
KNOWN_QUALIFIERS_RE='^(SNAPSHOT|RELEASE|FINAL|GA|SP[0-9]+)$'

# ---- ログ -----------------------------------------------------------------
log() {
  local level="$1"; shift
  local msg
  msg="$(date '+%Y-%m-%d %H:%M:%S') [${level}] $*"
  echo "${msg}"
  if [[ -n "${LOG_FILE}" ]]; then
    echo "${msg}" >> "${LOG_FILE}" 2>/dev/null || true
  fi
}
info()  { log "INFO"  "$@"; }
warn()  { log "WARN"  "$@"; }
error() { log "ERROR" "$@" >&2; }

die() { error "$@"; exit 1; }

# ---- usage ----------------------------------------------------------------
usage() {
  cat <<'EOF'
使い方:
  jar_manager.sh --mode <download|export|validate|report> [options]

モード:
  download   リストに基づき JAR をダウンロード・配置 (--list, --target-dir)
  export     ディレクトリを走査し再取り込み可能なリストを生成 (--scan-dir, --output)
  validate   リストの書式・必須項目を検証 (--list)
  report     ディレクトリを走査し Excel レポートを生成 (--scan-dir, --output)
             取得 URL・ライブラリ概要を出力。groupId/version 欠落時は推定補完。

オプション:
  --mode <mode>            動作モード (必須)
  --list <file>            入力リストファイル (download / validate)
  --target-dir <dir>       配置先ディレクトリ (list の targetDir 空欄時の既定)
  --scan-dir <dir>         export / report の走査対象ディレクトリ
  --output <file>          export のリスト / report の xlsx 出力先
  --engine <python|shell|java>   実装方式 (既定: python。report は python 固定)
  --dry-run                実際の操作をせず実行予定内容のみ表示
  --repo <url>             リポジトリ base URL (複数指定可、優先順)
  --timeout <sec>          HTTP タイムアウト秒 (既定: 30)
  --retry <n>              ダウンロード再試行回数 (既定: 3)
  --checksum-mode <warn|strict|skip>   チェックサム検証方針 (既定: warn)
  --allow-dir <dir>        許可する配置先 (path traversal 対策、複数可)
  --include-old            export / report で old 配下も対象にする (既定: 除外)
  --online                 report で Maven Central 照合により座標・概要を補完し
                           取得 URL を実在確認する (要ネットワーク)
  --log-file <file>        ログ出力ファイル (標準出力と併用)
  --help                   このヘルプ

例:
  ./jar_manager.sh --mode download --list jars.tsv --target-dir /opt/app/lib --engine python
  ./jar_manager.sh --mode download --list jars.tsv --target-dir /opt/app/lib --engine shell
  ./jar_manager.sh --mode export --scan-dir /opt/app/lib --output jars_export.tsv --engine python
  ./jar_manager.sh --mode validate --list jars.tsv
  ./jar_manager.sh --mode report --scan-dir /opt/app/lib --output jars_report.xlsx
  ./jar_manager.sh --mode report --scan-dir /opt/app/lib --output jars_report.xlsx --online
  ./jar_manager.sh --mode download --list jars.tsv --target-dir /opt/app/lib --dry-run
EOF
}

# ---- 引数解析 --------------------------------------------------------------
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode)          MODE="${2:-}"; shift 2;;
      --list)          LIST_FILE="${2:-}"; shift 2;;
      --target-dir)    TARGET_DIR="${2:-}"; shift 2;;
      --scan-dir)      SCAN_DIR="${2:-}"; shift 2;;
      --output)        OUTPUT="${2:-}"; shift 2;;
      --engine)        ENGINE="${2:-}"; shift 2;;
      --dry-run)       DRY_RUN="true"; shift;;
      --repo)          REPOS+=("${2:-}"); shift 2;;
      --timeout)       TIMEOUT="${2:-}"; shift 2;;
      --retry)         RETRY="${2:-}"; shift 2;;
      --checksum-mode) CHECKSUM_MODE="${2:-}"; shift 2;;
      --allow-dir)     ALLOW_DIRS+=("${2:-}"); shift 2;;
      --include-old)   INCLUDE_OLD="true"; shift;;
      --online)        ONLINE="true"; shift;;
      --log-file)      LOG_FILE="${2:-}"; shift 2;;
      --help|-h)       usage; exit 0;;
      *) die "不明なオプション: $1 (--help 参照)";;
    esac
  done

  [[ -n "${MODE}" ]] || { usage; die "--mode は必須です"; }
  case "${MODE}" in download|export|validate|report) ;; *) die "不正な --mode: ${MODE}";; esac
  case "${ENGINE}" in python|shell|java) ;; *) die "不正な --engine: ${ENGINE}";; esac
  case "${CHECKSUM_MODE}" in warn|strict|skip) ;; *) die "不正な --checksum-mode";; esac

  if [[ ${#REPOS[@]} -eq 0 ]]; then
    REPOS=("${DEFAULT_REPOS[@]}")
  fi
}

# ---- 依存チェック ----------------------------------------------------------
DL_TOOL=""
detect_downloader() {
  if command -v curl >/dev/null 2>&1; then
    DL_TOOL="curl"
  elif command -v wget >/dev/null 2>&1; then
    DL_TOOL="wget"
  else
    die "curl も wget も見つかりません"
  fi
}

# ============================================================================
# Python エンジンへの委譲
# ============================================================================
run_python_engine() {
  command -v python3 >/dev/null 2>&1 || die "python3 が見つかりません"
  [[ -f "${PY_SCRIPT}" ]] || die "jar_manager.py が見つかりません: ${PY_SCRIPT}"

  local -a args=(--mode "${MODE}")
  [[ -n "${LIST_FILE}"  ]] && args+=(--list "${LIST_FILE}")
  [[ -n "${TARGET_DIR}" ]] && args+=(--target-dir "${TARGET_DIR}")
  [[ -n "${SCAN_DIR}"   ]] && args+=(--scan-dir "${SCAN_DIR}")
  [[ -n "${OUTPUT}"     ]] && args+=(--output "${OUTPUT}")
  [[ -n "${LOG_FILE}"   ]] && args+=(--log-file "${LOG_FILE}")
  args+=(--timeout "${TIMEOUT}" --retry "${RETRY}" --checksum-mode "${CHECKSUM_MODE}")
  [[ "${DRY_RUN}"     == "true" ]] && args+=(--dry-run)
  [[ "${INCLUDE_OLD}" == "true" ]] && args+=(--include-old)
  [[ "${ONLINE}"      == "true" ]] && args+=(--online)
  local r
  for r in "${REPOS[@]}"; do args+=(--repo "${r}"); done
  local d
  for d in "${ALLOW_DIRS[@]}"; do args+=(--allow-dir "${d}"); done

  info "engine=python で実行します"
  python3 "${PY_SCRIPT}" "${args[@]}"
}

# ============================================================================
# Java エンジン: export の pom.properties 抽出のみ Java を利用
# ============================================================================
run_java_engine() {
  command -v java >/dev/null 2>&1 || die "engine=java 指定ですが java が見つかりません"
  info "engine=java: メタデータ抽出に Java を使用可能です"
  if [[ ! -f "${JAVA_SRC%.java}.class" && -f "${JAVA_SRC}" ]]; then
    if command -v javac >/dev/null 2>&1; then
      info "JarMetaReader をコンパイルします"
      javac -d "${SCRIPT_DIR}/java" "${JAVA_SRC}" || warn "javac 失敗。Python 抽出にフォールバック"
    fi
  fi
  # 実処理は Python 実装を再利用 (座標抽出は Python zipfile が確実なため)
  run_python_engine
}

# ============================================================================
# Shell エンジン: Bash のみで完結
# ============================================================================

# fileName を Maven 標準形式で解決
sh_resolve_filename() {
  # $1=artifactId $2=version $3=packaging $4=classifier $5=fileName
  local artifactId="$1" version="$2" packaging="$3" classifier="$4" fileName="$5"
  if [[ -n "${fileName}" ]]; then
    printf '%s' "${fileName}"; return
  fi
  local ext="${packaging:-jar}"
  if [[ -n "${classifier}" ]]; then
    printf '%s-%s-%s.%s' "${artifactId}" "${version}" "${classifier}" "${ext}"
  else
    printf '%s-%s.%s' "${artifactId}" "${version}" "${ext}"
  fi
}

# fileName の安全性検証 (path traversal 防止)
sh_validate_filename() {
  local name="$1"
  [[ -n "${name}" ]] || die "fileName が空です"
  case "${name}" in
    */*|*'\'*|..|.) die "不正な fileName: ${name}";;
  esac
}

# targetDir 検証 (--allow-dir があれば配下チェック)
sh_validate_target_dir() {
  local target="$1"
  [[ -n "${target}" ]] || die "targetDir が未指定です"
  local norm
  norm="$(cd "$(dirname "${target}")" 2>/dev/null && pwd)/$(basename "${target}")" || norm="${target}"
  # 正規化できない (未作成) 場合は文字列ベースで .. を拒否
  case "${target}" in
    *..*) warn "targetDir に '..' を含みます: ${target}";;
  esac
  if [[ ${#ALLOW_DIRS[@]} -gt 0 ]]; then
    local d ok="false"
    for d in "${ALLOW_DIRS[@]}"; do
      local ad
      ad="$(cd "${d}" 2>/dev/null && pwd || echo "${d}")"
      if [[ "${target}" == "${ad}" || "${target}" == "${ad}/"* ]]; then ok="true"; break; fi
    done
    [[ "${ok}" == "true" ]] || die "targetDir が許可された配置先の外です: ${target}"
  fi
  printf '%s' "${target}"
}

# ファイル名から artifactId / version / classifier を推定
# 出力: "artifactId<TAB>version<TAB>classifier"
sh_parse_jar_filename() {
  local base="$1"
  local stem="${base%.jar}"
  # ハイフン直後が数字の最初の位置を探す
  # sed で最初の -<digit> の前を artifactId, 後ろを rest とする
  if [[ ! "${stem}" =~ -[0-9] ]]; then
    printf '%s\t%s\t%s' "${stem}" "UNKNOWN" ""
    return
  fi
  local artifact rest
  # 最短一致で最初の -<digit> を境界にする
  artifact="$(printf '%s' "${stem}" | sed -E 's/^(.+?)-([0-9].*)$/\1/')"
  rest="$(printf '%s' "${stem}" | sed -E 's/^(.+?)-([0-9].*)$/\2/')"
  # sed の非貪欲 (?) は GNU sed 非対応のため、フォールバックで手動処理
  if [[ "${artifact}" == "${stem}" ]]; then
    # 貪欲でないマッチが効かない環境: 先頭から最初の -digit を探す
    local i=0 ch prev="" idx=-1
    for (( i=1; i<${#stem}; i++ )); do
      ch="${stem:i:1}"; prev="${stem:i-1:1}"
      if [[ "${prev}" == "-" && "${ch}" =~ [0-9] ]]; then idx=$((i-1)); break; fi
    done
    if [[ ${idx} -ge 0 ]]; then
      artifact="${stem:0:idx}"
      rest="${stem:idx+1}"
    else
      printf '%s\t%s\t%s' "${stem}" "UNKNOWN" ""; return
    fi
  fi
  # rest を version と classifier に分割
  local version="" classifier="" first="true" tok
  local IFS='-'
  read -ra toks <<< "${rest}"
  unset IFS
  for tok in "${toks[@]}"; do
    if [[ "${first}" == "true" ]]; then
      version="${tok}"; first="false"; continue
    fi
    local up
    up="$(printf '%s' "${tok}" | tr '[:lower:]' '[:upper:]')"
    if [[ "${up}" =~ ${KNOWN_QUALIFIERS_RE} ]]; then
      version="${version}-${tok}"
    elif [[ "${tok}" =~ ^[0-9] ]]; then
      version="${version}-${tok}"
    else
      if [[ -n "${classifier}" ]]; then classifier="${classifier}-${tok}"; else classifier="${tok}"; fi
    fi
  done
  printf '%s\t%s\t%s' "${artifact}" "${version}" "${classifier}"
}

# URL 組み立て
sh_build_url() {
  local repo="$1" groupId="$2" artifactId="$3" version="$4" fname="$5"
  local grouppath="${groupId//./\/}"
  printf '%s/%s/%s/%s/%s' "${repo%/}" "${grouppath}" "${artifactId}" "${version}" "${fname}"
}

# 存在確認 (HEAD -> GET フォールバック)
sh_http_exists() {
  local url="$1"
  if [[ "${DL_TOOL}" == "curl" ]]; then
    local code
    code="$(curl -sSL -o /dev/null -w '%{http_code}' -I --max-time "${TIMEOUT}" "${url}" 2>/dev/null || echo 000)"
    if [[ "${code}" =~ ^2|^3 ]]; then return 0; fi
    # HEAD 不可の場合 GET Range
    code="$(curl -sSL -o /dev/null -w '%{http_code}' -r 0-0 --max-time "${TIMEOUT}" "${url}" 2>/dev/null || echo 000)"
    [[ "${code}" =~ ^2|^3 ]]
  else
    wget -q --spider --timeout="${TIMEOUT}" "${url}" 2>/dev/null
  fi
}

# ダウンロード (一時ファイルへ)
sh_http_download() {
  local url="$1" out="$2"
  if [[ "${DL_TOOL}" == "curl" ]]; then
    curl -sSL --fail --max-time "${TIMEOUT}" -o "${out}" "${url}"
  else
    wget -q --timeout="${TIMEOUT}" -O "${out}" "${url}"
  fi
}

# テキスト取得 (チェックサム)
sh_http_text() {
  local url="$1"
  if [[ "${DL_TOOL}" == "curl" ]]; then
    curl -sSL --fail --max-time "${TIMEOUT}" "${url}" 2>/dev/null || true
  else
    wget -q --timeout="${TIMEOUT}" -O - "${url}" 2>/dev/null || true
  fi
}

# JAR (zip) 妥当性検査
sh_verify_jar() {
  local f="$1"
  if command -v unzip >/dev/null 2>&1; then
    unzip -qt "${f}" >/dev/null 2>&1
  elif command -v jar >/dev/null 2>&1; then
    jar tf "${f}" >/dev/null 2>&1
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "${f}" <<'PY' >/dev/null 2>&1
import sys, zipfile
try:
    z = zipfile.ZipFile(sys.argv[1]); sys.exit(0 if z.testzip() is None else 1)
except Exception:
    sys.exit(1)
PY
  else
    warn "zip 検査ツールが無いため JAR 妥当性検査をスキップ"
    return 0
  fi
}

# チェックサム検証 (戻り値: 0=一致, 1=不一致, 2=取得不可)
sh_verify_checksum() {
  local url="$1" file="$2"
  local algo ext remote local_hash cmd
  for pair in "sha256:.sha256" "sha1:.sha1"; do
    algo="${pair%%:*}"; ext="${pair##*:}"
    remote="$(sh_http_text "${url}${ext}")"
    remote="$(printf '%s' "${remote}" | awk '{print $1}' | tr '[:upper:]' '[:lower:]')"
    [[ -n "${remote}" ]] || continue
    case "${algo}" in
      sha256) cmd="sha256sum";;
      sha1)   cmd="sha1sum";;
    esac
    command -v "${cmd}" >/dev/null 2>&1 || continue
    local_hash="$(${cmd} "${file}" | awk '{print $1}')"
    if [[ "${local_hash}" == "${remote}" ]]; then return 0; else
      warn "${algo} 不一致: local=${local_hash} remote=${remote}"; return 1
    fi
  done
  return 2
}

# 既存 JAR を old へ退避
sh_evacuate_old() {
  local target_dir="$1" filename="$2" artifactId="$3"
  local parsed ver
  parsed="$(sh_parse_jar_filename "${filename}")"
  ver="$(printf '%s' "${parsed}" | cut -f2)"
  [[ -n "${ver}" ]] || ver="UNKNOWN"
  local old_dir="${target_dir}/old/${artifactId}/${ver}"
  local src="${target_dir}/${filename}"
  local dst="${old_dir}/${filename}"

  if [[ "${DRY_RUN}" == "true" ]]; then
    info "[dry-run] 退避予定: ${src} -> ${dst}"
    return 0
  fi
  mkdir -p "${old_dir}" || die "old ディレクトリを作成できません: ${old_dir}"
  if [[ -e "${dst}" ]]; then
    local ts; ts="$(date '+%Y%m%d%H%M%S')"
    local root="${filename%.jar}"
    dst="${old_dir}/${root}.${ts}.jar"
    warn "退避先が既存のためタイムスタンプ付与: ${dst}"
  fi
  mv "${src}" "${dst}"
  info "退避: ${src} -> ${dst}"
}

# 同一 artifactId の既存 JAR を列挙 (完全一致は除外)
sh_find_existing() {
  local target_dir="$1" artifactId="$2" classifier="$3" new_name="$4"
  [[ -d "${target_dir}" ]] || return 0
  local f base parsed art cls
  shopt -s nullglob
  for f in "${target_dir}"/*.jar; do
    base="$(basename "${f}")"
    [[ "${base}" == "${new_name}" ]] && continue
    parsed="$(sh_parse_jar_filename "${base}")"
    art="$(printf '%s' "${parsed}" | cut -f1)"
    cls="$(printf '%s' "${parsed}" | cut -f3)"
    if [[ "${art}" == "${artifactId}" && "${cls}" == "${classifier}" ]]; then
      printf '%s\n' "${base}"
    fi
  done
  shopt -u nullglob
}

# 1 エントリの download
sh_download_one() {
  local groupId="$1" artifactId="$2" version="$3" packaging="$4"
  local classifier="$5" targetDir="$6" fileName="$7"

  [[ -n "${groupId}" && -n "${artifactId}" && -n "${version}" ]] \
    || { error "必須項目 (groupId/artifactId/version) が不足"; return 1; }

  local eff_target="${targetDir:-${TARGET_DIR}}"
  eff_target="$(sh_validate_target_dir "${eff_target}")"
  local fname; fname="$(sh_resolve_filename "${artifactId}" "${version}" "${packaging}" "${classifier}" "${fileName}")"
  sh_validate_filename "${fname}"
  local dest="${eff_target}/${fname}"

  # 冪等性
  if [[ -f "${dest}" ]]; then
    if [[ "${CHECKSUM_MODE}" == "skip" ]]; then info "既存のためスキップ: ${dest}"; return 0; fi
    if sh_verify_jar "${dest}"; then info "既存 (妥当) のためスキップ: ${dest}"; return 0; fi
    warn "既存ファイルが破損。再取得します: ${dest}"
  fi

  # URL 解決
  local repo url found=""
  for repo in "${REPOS[@]}"; do
    url="$(sh_build_url "${repo}" "${groupId}" "${artifactId}" "${version}" "${fname}")"
    if [[ "${DRY_RUN}" == "true" ]]; then
      info "[dry-run] 存在確認予定: ${url}"; found="${url}"; break
    fi
    if sh_http_exists "${url}"; then found="${url}"; break; fi
  done
  [[ -n "${found}" ]] || { error "全リポジトリで未検出: ${groupId}:${artifactId}:${version}"; return 1; }

  if [[ "${DRY_RUN}" == "true" ]]; then
    info "[dry-run] 作成予定 dir : ${eff_target}"
    local old
    while IFS= read -r old; do
      [[ -n "${old}" ]] && sh_evacuate_old "${eff_target}" "${old}" "${artifactId}"
    done < <(sh_find_existing "${eff_target}" "${artifactId}" "${classifier}" "${fname}")
    info "[dry-run] DL 予定 URL  : ${found}"
    info "[dry-run] 配置予定 path: ${dest}"
    return 0
  fi

  mkdir -p "${eff_target}" || { error "配置先を作成できません: ${eff_target}"; return 1; }

  # 一時ファイルへ DL (リトライ)
  local tmp; tmp="$(mktemp "${eff_target}/.jm_XXXXXX.part")" || { error "一時ファイル作成失敗"; return 1; }
  # trap でクリーンアップ
  local ok="false" attempt
  for (( attempt=1; attempt<=RETRY; attempt++ )); do
    if sh_http_download "${found}" "${tmp}"; then ok="true"; break; fi
    warn "DL 失敗 (${attempt}/${RETRY}): ${found}"
  done
  if [[ "${ok}" != "true" ]]; then rm -f "${tmp}"; error "ダウンロード失敗: ${found}"; return 1; fi

  # zip 検査
  if ! sh_verify_jar "${tmp}"; then rm -f "${tmp}"; error "JAR が壊れています: ${found}"; return 1; fi

  # チェックサム
  if [[ "${CHECKSUM_MODE}" != "skip" ]]; then
    set +e; sh_verify_checksum "${found}" "${tmp}"; local cs=$?; set -e
    if [[ ${cs} -eq 1 && "${CHECKSUM_MODE}" == "strict" ]]; then
      rm -f "${tmp}"; error "チェックサム不一致 (strict): ${found}"; return 1
    elif [[ ${cs} -eq 2 ]]; then
      warn "チェックサム取得不可: ${found}"
      [[ "${CHECKSUM_MODE}" == "strict" ]] && { rm -f "${tmp}"; error "チェックサム取得不可 (strict)"; return 1; }
    fi
  fi

  # 新規 DL 成功後に既存を退避
  local old
  while IFS= read -r old; do
    [[ -n "${old}" ]] && sh_evacuate_old "${eff_target}" "${old}" "${artifactId}"
  done < <(sh_find_existing "${eff_target}" "${artifactId}" "${classifier}" "${fname}")

  chmod 0644 "${tmp}" 2>/dev/null || true
  mv -f "${tmp}" "${dest}"
  info "配置完了: ${dest}"
  return 0
}

# shell エンジン: download
sh_mode_download() {
  [[ -n "${LIST_FILE}" ]] || die "--list が必要です"
  [[ -f "${LIST_FILE}" ]] || die "リストファイルが存在しません: ${LIST_FILE}"
  detect_downloader
  info "engine=shell / downloader=${DL_TOOL} で実行します"

  local failures=0 total=0 lineno=0 line
  while IFS= read -r line || [[ -n "${line}" ]]; do
    lineno=$((lineno+1))
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    # TAB 優先、無ければカンマ
    local delim=$'\t'
    [[ "${line}" == *$'\t'* ]] || delim=','
    local IFS="${delim}"
    read -r g a v p c t f _rest <<< "${line}"
    unset IFS
    p="${p:-jar}"
    total=$((total+1))
    set +e
    sh_download_one "$(echo "$g"|xargs)" "$(echo "$a"|xargs)" "$(echo "$v"|xargs)" \
                    "$(echo "$p"|xargs)" "$(echo "$c"|xargs)" "$(echo "$t"|xargs)" "$(echo "$f"|xargs)"
    local rc=$?
    set -e
    [[ ${rc} -ne 0 ]] && { failures=$((failures+1)); error "行 ${lineno} 失敗"; }
  done < "${LIST_FILE}"

  if [[ ${failures} -gt 0 ]]; then error "download 完了: ${failures}/${total} 件失敗"; return 2; fi
  info "download 完了: 全 ${total} 件成功"
  return 0
}

# shell エンジン: export
sh_mode_export() {
  [[ -n "${SCAN_DIR}" && -n "${OUTPUT}" ]] || die "--scan-dir と --output が必要です"
  [[ -d "${SCAN_DIR}" ]] || die "scan-dir が存在しません: ${SCAN_DIR}"
  info "engine=shell で export します"

  local now; now="$(date '+%Y-%m-%d %H:%M:%S')"
  local tmp; tmp="$(mktemp)"
  {
    echo "# ==========================================================="
    echo "# jar_manager export list (再取り込み可能な TSV 形式)"
    echo "# generated : ${now}"
    echo "# scan-dir  : ${SCAN_DIR}"
    printf '# columns   : %s\n' "groupId	artifactId	version	packaging	classifier	targetDir	fileName"
    echo "# groupId が UNKNOWN の行は座標未確定。validate 前に手動補正を推奨。"
    echo "# ==========================================================="
    printf '# %s\n' "groupId	artifactId	version	packaging	classifier	targetDir	fileName"
  } > "${tmp}"

  # old 除外制御
  local prune=""
  [[ "${INCLUDE_OLD}" == "true" ]] || prune="-path */old/* -prune -o"

  # shellcheck disable=SC2086
  find "${SCAN_DIR}" ${prune} -type f -name '*.jar' -print 2>/dev/null | sort | while IFS= read -r jar; do
    local base parsed art ver cls group tdir
    base="$(basename "${jar}")"
    parsed="$(sh_parse_jar_filename "${base}")"
    art="$(printf '%s' "${parsed}" | cut -f1)"
    ver="$(printf '%s' "${parsed}" | cut -f2)"
    cls="$(printf '%s' "${parsed}" | cut -f3)"
    group="UNKNOWN"
    # pom.properties 抽出 (unzip があれば)
    if command -v unzip >/dev/null 2>&1; then
      local pp
      pp="$(unzip -Z1 "${jar}" 2>/dev/null | grep -E 'META-INF/maven/.*/pom.properties' | head -n1 || true)"
      if [[ -n "${pp}" ]]; then
        local content; content="$(unzip -p "${jar}" "${pp}" 2>/dev/null || true)"
        local g2 a2 v2
        g2="$(printf '%s' "${content}" | grep -E '^groupId=' | head -n1 | cut -d= -f2- | tr -d '\r')"
        a2="$(printf '%s' "${content}" | grep -E '^artifactId=' | head -n1 | cut -d= -f2- | tr -d '\r')"
        v2="$(printf '%s' "${content}" | grep -E '^version=' | head -n1 | cut -d= -f2- | tr -d '\r')"
        [[ -n "${g2}" ]] && group="${g2}"
        [[ -n "${a2}" ]] && art="${a2}"
        [[ -n "${v2}" ]] && ver="${v2}"
      fi
    fi
    tdir="$(dirname "${jar}")"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${group}" "${art}" "${ver}" "jar" "${cls}" "${tdir}" "${base}" >> "${tmp}"
  done

  mv -f "${tmp}" "${OUTPUT}"
  local n; n="$(grep -cvE '^\s*#|^\s*$' "${OUTPUT}" || echo 0)"
  info "エクスポート完了: ${OUTPUT} (${n} 件)"
  return 0
}

# shell エンジン: validate
sh_mode_validate() {
  [[ -n "${LIST_FILE}" ]] || die "--list が必要です"
  [[ -f "${LIST_FILE}" ]] || die "リストファイルが存在しません: ${LIST_FILE}"
  info "engine=shell で validate します"

  local errors=0 warnings=0 total=0 lineno=0 line
  while IFS= read -r line || [[ -n "${line}" ]]; do
    lineno=$((lineno+1))
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    local delim=$'\t'
    [[ "${line}" == *$'\t'* ]] || delim=','
    local IFS="${delim}"
    read -r g a v p c t f _rest <<< "${line}"
    unset IFS
    total=$((total+1))
    [[ -n "${a// /}" ]] || { error "行 ${lineno}: artifactId が空"; errors=$((errors+1)); }
    if [[ -z "${v// /}" ]]; then error "行 ${lineno}: version が空"; errors=$((errors+1));
    elif [[ ! "${v// /}" =~ ^[0-9][0-9A-Za-z._-]*$ ]]; then error "行 ${lineno}: version 形式が不正: ${v}"; errors=$((errors+1)); fi
    if [[ -z "${g// /}" ]]; then error "行 ${lineno}: groupId が空"; errors=$((errors+1));
    elif [[ "${g// /}" == "UNKNOWN" ]]; then warn "行 ${lineno}: groupId が UNKNOWN"; warnings=$((warnings+1)); fi
    if [[ -z "${t// /}" && -z "${TARGET_DIR}" ]]; then error "行 ${lineno}: targetDir 未指定"; errors=$((errors+1)); fi
  done < "${LIST_FILE}"

  info "validate 完了: ${total} 件, エラー ${errors}, 警告 ${warnings}"
  [[ ${errors} -gt 0 ]] && return 1
  return 0
}

run_shell_engine() {
  case "${MODE}" in
    download) sh_mode_download;;
    export)   sh_mode_export;;
    validate) sh_mode_validate;;
  esac
}

# ============================================================================
# main
# ============================================================================
main() {
  parse_args "$@"
  # report は Excel 生成のため Python 実装固定 (shell/java 実装は持たない)。
  if [[ "${MODE}" == "report" ]]; then
    [[ "${ENGINE}" != "python" ]] && \
      info "report は Python エンジンで実行します (engine=${ENGINE} 指定は無視)"
    run_python_engine
    return
  fi
  case "${ENGINE}" in
    python) run_python_engine;;
    shell)  run_shell_engine;;
    java)   run_java_engine;;
  esac
}

main "$@"
