#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# make_demo_jars.py
#   report モード (jar_report.py) の動作確認用に、メタデータの揃い方が異なる
#   ダミー JAR を生成する。ネットワーク不要・標準ライブラリのみ。
#
#   使い方:
#     python3 samples/make_demo_jars.py <出力ディレクトリ>
#
#   生成される JAR (座標解決の各経路を網羅):
#     log4j-core-2.22.1.jar : pom.properties + pom.xml + MANIFEST 完備
#     slf4j-api-2.0.9.jar   : MANIFEST (OSGi Bundle-*) のみ
#     guava-32.1.3-jre.jar  : groupId 無し → 既知マッピングで推定
#     commons-lang3.jar     : version 無し → MANIFEST から補完
#     ojdbc11-23.3.0.0.jar  : 商用ベンダー製 (Oracle・独自ライセンス) → ベンダー系
#     mystery-lib-1.0.jar   : 手掛かり乏しく groupId 未確定 (UNKNOWN)
#     old/.../log4j-core-2.17.1.jar : old 配下 (既定では走査対象外)
# ============================================================================
import os
import sys
import zipfile


def _jar(out_dir, name, entries):
    path = os.path.join(out_dir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry_name, content in entries.items():
            zf.writestr(entry_name, content)
    print("wrote", path)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)

    _jar(out_dir, "log4j-core-2.22.1.jar", {
        "META-INF/MANIFEST.MF":
            "Manifest-Version: 1.0\r\n"
            "Implementation-Title: Apache Log4j Core\r\n"
            "Implementation-Vendor: The Apache Software Foundation\r\n"
            "Implementation-Version: 2.22.1\r\n"
            "Bundle-License: https://www.apache.org/licenses/LICENSE-2.0.txt\r\n",
        "META-INF/maven/org.apache.logging.log4j/log4j-core/pom.properties":
            "groupId=org.apache.logging.log4j\n"
            "artifactId=log4j-core\nversion=2.22.1\n",
        "META-INF/maven/org.apache.logging.log4j/log4j-core/pom.xml":
            "<project><name>Apache Log4j Core</name>"
            "<description>The Apache Log4j Implementation</description>"
            "<url>https://logging.apache.org/log4j/2.x/</url>"
            "<licenses><license><name>Apache-2.0</name></license></licenses>"
            "<groupId>org.apache.logging.log4j</groupId>"
            "<artifactId>log4j-core</artifactId>"
            "<version>2.22.1</version></project>",
    })

    _jar(out_dir, "slf4j-api-2.0.9.jar", {
        "META-INF/MANIFEST.MF":
            "Manifest-Version: 1.0\r\n"
            "Bundle-Name: slf4j-api\r\n"
            "Bundle-SymbolicName: slf4j.api\r\n"
            "Bundle-Version: 2.0.9\r\n"
            "Bundle-Vendor: QOS.ch\r\n"
            "Bundle-Description: The slf4j API\r\n",
    })

    _jar(out_dir, "guava-32.1.3-jre.jar", {
        "META-INF/MANIFEST.MF":
            "Manifest-Version: 1.0\r\n"
            "Automatic-Module-Name: com.google.common\r\n",
    })

    _jar(out_dir, "commons-lang3.jar", {
        "META-INF/MANIFEST.MF":
            "Manifest-Version: 1.0\r\n"
            "Implementation-Title: Apache Commons Lang\r\n"
            "Implementation-Version: 3.14.0\r\n"
            "Implementation-Vendor: The Apache Software Foundation\r\n",
    })

    _jar(out_dir, "ojdbc11-23.3.0.0.jar", {
        "META-INF/MANIFEST.MF":
            "Manifest-Version: 1.0\r\n"
            "Implementation-Title: JDBC\r\n"
            "Implementation-Vendor: Oracle Corporation\r\n"
            "Implementation-Version: 23.3.0.0\r\n"
            "Bundle-License: Oracle Free Use Terms and Conditions\r\n",
        "META-INF/maven/com.oracle.database.jdbc/ojdbc11/pom.properties":
            "groupId=com.oracle.database.jdbc\n"
            "artifactId=ojdbc11\nversion=23.3.0.0\n",
    })

    _jar(out_dir, "mystery-lib-1.0.jar", {
        "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\n",
    })

    _jar(out_dir, os.path.join("old", "log4j-core", "2.17.1",
                               "log4j-core-2.17.1.jar"), {
        "META-INF/maven/org.apache.logging.log4j/log4j-core/pom.properties":
            "groupId=org.apache.logging.log4j\n"
            "artifactId=log4j-core\nversion=2.17.1\n",
    })


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python3 samples/make_demo_jars.py <出力ディレクトリ>",
              file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
