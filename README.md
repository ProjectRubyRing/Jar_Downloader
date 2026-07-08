# jar_manager — RHEL9/EC2 向け JAR ダウンロード・退避・一覧管理ツール

Maven 座標に基づき、複数の Java OSS JAR を指定バージョンでダウンロード・配置し、
既存 JAR を `old/` へ退避、配置済み JAR を再取り込み可能なリストとして export できる実用ツール一式。

- `--engine python` … シェルから Python を呼ぶ標準実装（推奨）
- `--engine shell`  … Bash のみで完結する実装
- `--engine java`   … export のメタデータ抽出で Java を補助利用（無ければ明確にエラー）

さらに `--mode report` で、配置済み JAR を走査して **取得 URL・ライブラリ概要・
Maven 座標を美しい Excel (.xlsx) に出力** できる（`jar_report.py` / 要 `openpyxl`）。
groupId やバージョンが欠落していても、ファイル名・既知マッピング・（`--online` 時は）
Maven Central 照合から座標を推定して補完する。

## 1. ディレクトリ構成

```
Jar_Downloader/
├── jar_manager.sh              # メイン実行スクリプト（--engine で方式切替）
├── jar_manager.py              # Python 補助実装（標準エンジン・標準ライブラリのみ）
├── jar_report.py               # report モード実装（Excel 出力・要 openpyxl）
├── java/
│   └── JarMetaReader.java      # Java 補助（pom.properties 抽出サンプル）
├── samples/
│   ├── jars.tsv                # 入力リストサンプル
│   ├── jars_export.tsv         # export 生成例
│   ├── make_demo_jars.py       # report 動作確認用のダミー JAR 生成
│   └── jars_report_sample.xlsx # report 出力サンプル
└── README.md
```

配置先（例 `/opt/app/lib`）に生成される退避構成:

```
/opt/app/lib/
├── log4j-core-2.22.1.jar           # 新バージョン
└── old/
    └── log4j-core/                 # バージョンを除いた名前（=artifactId）
        └── 2.17.1/                 # 既存 JAR のバージョン
            └── log4j-core-2.17.1.jar
```

## 2. 入力リスト形式（TSV）

`groupId  artifactId  version  packaging  classifier  targetDir  fileName`（TAB 区切り、CSV も可）

- 1 行 1 JAR、`#` 始まりはコメント、空行は無視。
- `classifier` 無しは空欄可。`packaging` は通常 `jar`。
- `fileName` 空欄 → Maven 標準名を自動生成（`artifactId-version[-classifier].jar`）。
- `targetDir` 空欄 → CLI の `--target-dir` を使用。
- 入力と出力（export）で同一形式。ヘッダもコメント行なので **export 結果をそのまま再入力可能**。

## 3. CLI

```
./jar_manager.sh --mode <download|export|validate> [options]
```

| オプション | 説明 |
|---|---|
| `--mode` | `download` / `export` / `validate` / `report`（必須）|
| `--list` | 入力リスト（download / validate）|
| `--target-dir` | 配置先。list の targetDir 空欄時の既定 |
| `--scan-dir` | export / report の走査対象 |
| `--output` | export のリスト / report の xlsx 出力先 |
| `--online` | report で Maven Central 照合による座標・概要補完と URL 実在確認 |
| `--engine` | `python`（既定）/ `shell` / `java` |
| `--dry-run` | 実操作せず予定内容のみ表示 |
| `--repo` | リポジトリ base URL（複数可・優先順）|
| `--timeout` | HTTP タイムアウト秒（既定 30）|
| `--retry` | DL 再試行回数（既定 3）|
| `--checksum-mode` | `warn`（既定）/ `strict` / `skip` |
| `--allow-dir` | 許可する配置先（path traversal 対策・複数可）|
| `--include-old` | export で old 配下も対象（既定は除外）|
| `--log-file` | ログ出力ファイル（標準出力と併用）|
| `--help` | ヘルプ |

## 4. 実行例

```bash
# ダウンロード（Python エンジン）
./jar_manager.sh --mode download --list samples/jars.tsv --target-dir /opt/app/lib --engine python

# ダウンロード（Shell エンジンのみで完結）
./jar_manager.sh --mode download --list samples/jars.tsv --target-dir /opt/app/lib --engine shell

# 既存 JAR を走査してリスト生成（再取り込み可能）
./jar_manager.sh --mode export --scan-dir /opt/app/lib --output jars_export.tsv --engine python

# 配置済み JAR を Excel レポート化（取得 URL・ライブラリ概要・座標推定を出力）
./jar_manager.sh --mode report --scan-dir /opt/app/lib --output jars_report.xlsx
# ネットワークを使い Maven Central 照合で座標・概要を補完し URL を実在確認
./jar_manager.sh --mode report --scan-dir /opt/app/lib --output jars_report.xlsx --online

# リスト検証
./jar_manager.sh --mode validate --list samples/jars.tsv

# ドライラン（作成/退避/DL URL/配置先を表示するのみ）
./jar_manager.sh --mode download --list samples/jars.tsv --target-dir /opt/app/lib --dry-run

# 配置先をホワイトリスト化 + チェックサム厳格
./jar_manager.sh --mode download --list samples/jars.tsv --target-dir /opt/app/lib \
  --allow-dir /opt/app/lib --checksum-mode strict --log-file /var/log/jar_manager.log
```

## 4.1 report モード（JAR インベントリの Excel 出力）

指定ディレクトリ配下の JAR を再帰的に走査し、1 JAR = 1 行の一覧を美しい Excel
（`.xlsx`）に出力する。`jar_report.py` が担当し、Excel 生成に **`openpyxl`** を用いる
（`pip install openpyxl`）。座標解決・メタデータ抽出部分は標準ライブラリのみで動作。

**出力カラム**: `No. / ファイル名 / groupId / artifactId / version / classifier /
packaging / ライブラリ名 / 概要 / 提供元 / ライセンス / プロジェクト URL /
座標の判定元 / 推定 / 取得 URL / 取得元リポジトリ / URL 確認 / サイズ / SHA-1 /
配置ディレクトリ / 備考`。「凡例」シートに色分けとカラム説明を同梱。

**座標の解決順（groupId / version が欠落していても推測）**:

1. JAR 内 `META-INF/maven/**/pom.properties`（最も確実）
2. JAR 内 `META-INF/maven/**/pom.xml`（parent の groupId/version 継承も考慮）
3. `MANIFEST.MF`（`Implementation-*` / `Bundle-*` から version 等を補完）
4. 既知アーティファクト → groupId マッピング（オフライン）
5. `--online` 時のみ Maven Central へ **SHA-1 照合**（リネーム済みでも正確に特定）→
   座標照合、および `pom` 取得による概要・ライセンス補完
6. ファイル名からの推定（`artifactId-version[-classifier].jar`）

**取得 URL**: 解決した座標から Maven 標準レイアウトで生成。`--online` 時は各リポジトリへ
`HEAD` で実在確認し、実在した最初のものを採用（未検出・offline・座標未確定は「URL 確認」
列に明示）。

**行の色分け**: groupId 未確定（`UNKNOWN`）＝橙、座標を推定補完＝黄、通常＝交互の縞。

```bash
# 動作確認（ネットワーク不要）: ダミー JAR を生成して Excel を作る
python3 samples/make_demo_jars.py /tmp/lib_demo
./jar_manager.sh --mode report --scan-dir /tmp/lib_demo --output /tmp/jars_report.xlsx
```

出力例は `samples/jars_report_sample.xlsx` を参照。

## 5. エラー時の挙動と終了コード

| 状況 | 挙動 |
|---|---|
| 全リポジトリで JAR 未検出 | 当該行を失敗として記録、処理継続、最後に非0終了 |
| ダウンロード途中失敗 | `--retry` 回まで再試行。最終失敗時は一時 `.part` を削除（中途ファイルを残さない）|
| JAR が壊れている（zip 不正）| 破棄して失敗扱い。既存 JAR は退避しない |
| チェックサム不一致 | `warn`=警告継続 / `strict`=失敗 / `skip`=検証しない |
| 既存が目的物と同一 | スキップ（冪等性）。破損時のみ再取得 |
| 退避先に同名ファイル | 上書きせずタイムスタンプ付与で回避 |
| 権限不足で配置先作成不可 | 明確なエラーで当該行失敗 |

終了コード: `0`=成功 / `1`=引数・入力エラー / `2`=一部 DL 失敗 / `130`=中断。

**ロールバック安全性**: 新 JAR のダウンロード・検証が成功した**後**に既存 JAR を退避するため、
DL 失敗時に既存 JAR が失われることはない。

## 6. RHEL9 事前準備

```bash
# 必須: bash / coreutils は標準搭載。以下を確認・導入。
sudo dnf install -y python3 curl unzip
# wget を使う場合（curl があれば不要）
sudo dnf install -y wget
# Java 補助（--engine java）を使う場合のみ
sudo dnf install -y java-17-openjdk-devel
# report モード（Excel 出力）を使う場合のみ
python3 -m pip install --user openpyxl

# 実行権限
chmod +x jar_manager.sh

# 動作確認（ネットワーク不要）
./jar_manager.sh --mode validate --list samples/jars.tsv --target-dir /opt/app/lib
```

`/opt/app/lib` など root 所有の配置先へ書き込む場合は `sudo` 実行、
または事前に `sudo chown $(whoami) /opt/app/lib` で権限を付与する。

## 7. 注意点・制約

- **バージョン判定の限界**: ファイル名推定は「最初のハイフン直後の数字」をバージョン開始とみなす。
  `guava-32.1.3-jre.jar` の `-jre` のようにバージョン一部がクラシファイア扱いになる等の誤判定があり得る。
  正確性が必要な場合は JAR 内 `pom.properties` を優先し、無ければ `UNKNOWN` を出力して validate で検知する。
- **download は list の座標をそのまま使う**ため、上記の推定限界は download には影響しない（export のみ）。
- **セキュリティ**: `fileName` にパス区切り・`..` を禁止（path traversal 防止）。`--allow-dir` 指定時は
  配置先をホワイトリスト外なら拒否。shell 実装は全変数をダブルクォート、`eval` 不使用、一時ファイルは `mktemp`。
- **チェックサム**: `.sha256` → `.sha1` の順で取得し検証。取得不可はリポジトリ仕様により起こり得るため既定は `warn`。
- **リポジトリ**: Maven Central を第一候補に、Apache/Spring/JBoss/Sonatype Releases を順に試行。任意サイトは対象外。

## 8. 拡張案

- Nexus/Artifactory など社内リポジトリの `--repo` 追加と Basic 認証対応。
- `maven-metadata.xml` を参照した `LATEST`/`RELEASE` 解決。
- GPG 署名（`.asc`）検証の追加。
- 並列ダウンロード（`xargs -P` / `concurrent.futures`）。
- systemd タイマーによる定期同期。
