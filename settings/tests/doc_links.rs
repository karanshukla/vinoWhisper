//! Every `` `tests::name` `` a comment cites, in any of the three crates, must
//! be a `#[test]` fn, so a rule that names its test fails CI when the test is
//! renamed or deleted. rustdoc can't check these: `#[test]` fns are compiled
//! out of the docs. See docs/comment-style.md.

use std::fs;
use std::path::{Path, PathBuf};

const CRATES: [&str; 3] = ["../gui", "../tray", "."];

#[test]
fn every_cited_test_exists() {
    let mut broken = Vec::new();
    for krate in CRATES {
        let src = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(krate)
            .join("src");
        for file in rust_files(&src) {
            let text = fs::read_to_string(&file).unwrap();
            for (line_no, line) in text.lines().enumerate() {
                if !line.trim_start().starts_with("//") {
                    continue;
                }
                for cited in cited_tests(line) {
                    if !resolves(&src, &file, cited) {
                        broken.push(format!("{}:{}: `{cited}`", file.display(), line_no + 1));
                    }
                }
            }
        }
    }
    assert!(broken.is_empty(), "no such test:\n{}", broken.join("\n"));
}

#[test]
fn the_check_finds_a_cited_test_and_rejects_a_missing_one() {
    let text = "// (pinned by `tests::real`)\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn real() {}\n    fn helper() {}\n}\n";
    assert_eq!(cited_tests(text.lines().next().unwrap()), ["tests::real"]);
    assert!(is_test_fn(text, "real"));
    assert!(!is_test_fn(text, "helper"), "not a #[test]");
    assert!(!is_test_fn(text, "gone"));
}

#[test]
fn a_citation_resolves_in_a_modules_separate_tests_file() {
    let src = Path::new(env!("CARGO_MANIFEST_DIR")).join("../gui/src");
    let window = src.join("window.rs");
    let a_test = first_test_in(&src.join("window/tests.rs"));
    assert!(resolves(&src, &window, &format!("tests::{a_test}")));
    assert!(resolves(
        &src,
        &src.join("main.rs"),
        &format!("window::tests::{a_test}")
    ));
    assert!(!resolves(&src, &window, "tests::no_such_test"));
}

fn first_test_in(file: &Path) -> String {
    let text = fs::read_to_string(file).unwrap();
    let mut lines = text.lines().map(str::trim);
    lines.find(|l| *l == "#[test]").unwrap();
    let signature = lines.find(|l| l.starts_with("fn ")).unwrap();
    signature["fn ".len()..signature.find('(').unwrap()].to_owned()
}

fn rust_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in fs::read_dir(dir).unwrap().flatten() {
        let path = entry.path();
        if path.is_dir() {
            files.extend(rust_files(&path));
        } else if path.extension().is_some_and(|e| e == "rs") {
            files.push(path);
        }
    }
    files
}

/// Code spans like `` `tests::name` `` or `` `module::tests::name` ``.
fn cited_tests(line: &str) -> Vec<&str> {
    line.split('`')
        .skip(1)
        .step_by(2)
        .filter(|span| {
            span.contains("tests::")
                && span
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == ':')
        })
        .collect()
}

/// `tests::name` is in `file` or its `<file>/tests.rs`; `module::tests::name`
/// is in `src/module.rs` (or `module/mod.rs`) or `src/module/tests.rs`.
fn resolves(src: &Path, file: &Path, cited: &str) -> bool {
    let (module, name) = cited.rsplit_once("tests::").unwrap();
    let module = module.trim_end_matches("::");
    let owner = if module.is_empty() {
        file.to_path_buf()
    } else {
        let path = src.join(module.replace("::", "/"));
        let flat = path.with_extension("rs");
        if flat.exists() {
            flat
        } else {
            path.join("mod.rs")
        }
    };
    let separate = owner.with_extension("").join("tests.rs");
    [owner, separate]
        .iter()
        .any(|f| fs::read_to_string(f).is_ok_and(|text| is_test_fn(&text, name)))
}

/// A `fn name(` whose attributes, above it, include `#[test]` or
/// `#[tokio::test]`.
fn is_test_fn(text: &str, name: &str) -> bool {
    let lines: Vec<&str> = text.lines().collect();
    let signature = [format!("fn {name}("), format!("async fn {name}(")];
    lines.iter().enumerate().any(|(i, line)| {
        signature
            .iter()
            .any(|s| line.trim_start().starts_with(s.as_str()))
            && lines[..i]
                .iter()
                .rev()
                .map(|l| l.trim())
                .take_while(|l| l.starts_with("#[") || l.starts_with("///"))
                .any(|l| l == "#[test]" || l.starts_with("#[tokio::test"))
    })
}
