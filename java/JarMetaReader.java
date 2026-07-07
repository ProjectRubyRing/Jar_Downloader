// ============================================================================
// JarMetaReader.java
//   Java 補助実装 (--engine java 時に利用可能)。
//   JAR 内の META-INF/maven/**/pom.properties を読み取り、
//   groupId / artifactId / version を TSV で標準出力する。
//
//   ビルド : javac -d . JarMetaReader.java
//   実行   : java JarMetaReader <jarファイル> [<jarファイル> ...]
//   出力   : 1 JAR につき 1 行
//            groupId<TAB>artifactId<TAB>version<TAB>packaging<TAB>classifier<TAB>fileName
//
//   pom.properties が無い場合はファイル名から artifactId/version を推定し、
//   groupId は UNKNOWN とする。
//
//   ※ 本ツールの主エンジンは Python (jar_manager.py) 側の zipfile 抽出であり、
//     本クラスは「Java が利用可能な場合の補助」という位置づけ。
// ============================================================================
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Paths;
import java.util.Enumeration;
import java.util.Properties;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

public final class JarMetaReader {

    // ハイフン直後が数字の最初の位置をバージョン開始とみなす正規表現。
    // artifactId にハイフンを含むケース (log4j-core 等) に対応する。
    private static final Pattern VER_START = Pattern.compile("-(?=\\d)");

    public static void main(String[] args) {
        if (args.length == 0) {
            System.err.println("usage: java JarMetaReader <jar> [<jar> ...]");
            System.exit(2);
        }
        int failures = 0;
        for (String path : args) {
            try {
                System.out.println(readOne(path));
            } catch (Exception e) {
                System.err.println("ERROR " + path + ": " + e.getMessage());
                failures++;
            }
        }
        System.exit(failures > 0 ? 1 : 0);
    }

    private static String readOne(String path) throws IOException {
        String fileName = Paths.get(path).getFileName().toString();
        String[] parsed = parseFileName(fileName); // artifactId, version, classifier
        String group = "UNKNOWN";
        String artifact = parsed[0];
        String version = parsed[1];
        String classifier = parsed[2];

        try (ZipFile zf = new ZipFile(path)) {
            Enumeration<? extends ZipEntry> entries = zf.entries();
            while (entries.hasMoreElements()) {
                ZipEntry ze = entries.nextElement();
                String name = ze.getName();
                if (name.startsWith("META-INF/maven/") && name.endsWith("pom.properties")) {
                    Properties props = new Properties();
                    try (InputStream in = zf.getInputStream(ze);
                         BufferedReader br = new BufferedReader(
                                 new InputStreamReader(in, StandardCharsets.UTF_8))) {
                        props.load(br);
                    }
                    String g = props.getProperty("groupId");
                    String a = props.getProperty("artifactId");
                    String v = props.getProperty("version");
                    if (g != null && !g.isEmpty()) group = g;
                    if (a != null && !a.isEmpty()) artifact = a;
                    if (v != null && !v.isEmpty()) version = v;
                    break; // 最初の pom.properties を採用
                }
            }
        }

        return String.join("\t",
                group, artifact, version, "jar", classifier, fileName);
    }

    // ファイル名から artifactId / version / classifier を推定する。
    // 判別不能時は version="UNKNOWN"。
    static String[] parseFileName(String fileName) {
        String stem = fileName.toLowerCase().endsWith(".jar")
                ? fileName.substring(0, fileName.length() - 4)
                : fileName;
        Matcher m = VER_START.matcher(stem);
        if (!m.find()) {
            return new String[]{stem, "UNKNOWN", ""};
        }
        int idx = m.start();
        String artifact = stem.substring(0, idx);
        String rest = stem.substring(idx + 1); // 例: 2.17.1 / 3.0.0-SNAPSHOT / 2.15.3-sources

        String[] parts = rest.split("-");
        StringBuilder version = new StringBuilder(parts[0]);
        StringBuilder classifier = new StringBuilder();
        for (int i = 1; i < parts.length; i++) {
            String tok = parts[i];
            String up = tok.toUpperCase();
            boolean isQualifier = up.equals("SNAPSHOT") || up.equals("RELEASE")
                    || up.equals("FINAL") || up.equals("GA") || up.matches("SP[0-9]+");
            if (isQualifier || (!tok.isEmpty() && Character.isDigit(tok.charAt(0)))) {
                version.append('-').append(tok);
            } else {
                if (classifier.length() > 0) classifier.append('-');
                classifier.append(tok);
            }
        }
        return new String[]{artifact, version.toString(), classifier.toString()};
    }
}
