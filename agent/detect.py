"""Detect the project technology and infer install/test/lint/type-check/build commands.

Detection is deliberately conservative: a command is only suggested when the project
shows evidence that the tool is in use. Anything can be overridden in .github/agent.yml.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".cache", "vendor", ".next", ".nuxt", ".idea", ".vscode", "target", ".gradle", ".tox",
}

MANIFESTS = [
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements.txt",
    "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "composer.json",
    "composer.lock", "pom.xml", "build.gradle", "build.gradle.kts",
]


@dataclass
class ProjectInfo:
    types: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    typecheck: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    has_tests: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if not self.types:
            return "unknown / empty project (no manifest detected yet)"
        return ", ".join(self.types)


def _walk(ws: Path, max_files: int = 4000):
    count = 0
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for name in filenames:
            yield Path(dirpath) / name
            count += 1
            if count >= max_files:
                return


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _any_file(ws: Path, pred) -> bool:
    return any(pred(p) for p in _walk(ws))


def _detect_python(ws: Path, info: ProjectInfo) -> None:
    markers = ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile"]
    has_py = any(p.suffix == ".py" for p in _walk(ws, 600))
    if not (any((ws / m).exists() for m in markers) or has_py):
        return
    info.types.append("python")
    blob = "".join(_read(ws / m) for m in ("requirements.txt", "requirements-dev.txt", "pyproject.toml",
                                             "setup.cfg", "Pipfile", "tox.ini"))
    if (ws / "requirements.txt").exists():
        info.install.append("pip install -r requirements.txt")
    if (ws / "pyproject.toml").exists() and ("[project]" in blob or "[tool.poetry]" in blob) or (ws / "setup.py").exists():
        info.install.append("pip install -e .")
    py_tests = [p for p in _walk(ws) if p.suffix == ".py" and (p.name.startswith("test_") or p.name.endswith("_test.py"))]
    if py_tests:
        info.has_tests = True
        use_pytest = ("pytest" in blob.lower() or (ws / "pytest.ini").exists() or (ws / "conftest.py").exists())
        if use_pytest:
            info.test.append("python -m pytest -q")
        else:
            test_dir = next((d for d in ("tests", "test") if (ws / d).is_dir()), None)
            info.test.append(f"python -m unittest discover -s {test_dir} -v" if test_dir else "python -m unittest discover -v")
    if "ruff" in blob.lower() or (ws / "ruff.toml").exists():
        info.lint.append("ruff check .")
    elif (ws / ".flake8").exists() or "flake8" in blob.lower():
        info.lint.append("flake8")
    if (ws / "mypy.ini").exists() or "[tool.mypy]" in blob or "mypy" in blob.lower():
        info.typecheck.append("mypy .")


def _detect_node(ws: Path, info: ProjectInfo) -> None:
    pkg_path = ws / "package.json"
    if not pkg_path.exists():
        return
    try:
        pkg = json.loads(_read(pkg_path) or "{}")
    except json.JSONDecodeError:
        info.notes.append("package.json is not valid JSON")
        pkg = {}
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts", {}) or {}
    info.types.append("node")
    if (ws / "tsconfig.json").exists() or "typescript" in deps:
        info.types.append("typescript")
    else:
        info.types.append("javascript")
    if "react" in deps:
        info.types.append("react")
    if "next" in deps:
        info.types.append("nextjs")
    if "express" in deps and ("mongoose" in deps or "mongodb" in deps):
        info.types.append("mern-backend")
    pm = "pnpm" if (ws / "pnpm-lock.yaml").exists() else "yarn" if (ws / "yarn.lock").exists() else "npm"
    run = {"npm": "npm run", "yarn": "yarn", "pnpm": "pnpm run"}[pm]
    info.install.append(f"{pm} install")
    test_script = str(scripts.get("test", ""))
    if test_script and "no test specified" not in test_script:
        info.has_tests = True
        info.test.append("npm test" if pm == "npm" else f"{pm} test")
    if "lint" in scripts:
        info.lint.append(f"{run} lint")
    for name in ("typecheck", "type-check", "tsc"):
        if name in scripts:
            info.typecheck.append(f"{run} {name}")
            break
    else:
        if (ws / "tsconfig.json").exists() and "typescript" in deps:
            info.typecheck.append("npx tsc --noEmit")
    if "build" in scripts:
        info.build.append(f"{run} build")


def _detect_php(ws: Path, info: ProjectInfo) -> None:
    composer = ws / "composer.json"
    has_php = composer.exists() or (ws / "artisan").exists() or any(p.suffix == ".php" for p in _walk(ws, 600))
    if not has_php:
        return
    info.types.append("php")
    comp = {}
    if composer.exists():
        try:
            comp = json.loads(_read(composer) or "{}")
        except json.JSONDecodeError:
            info.notes.append("composer.json is not valid JSON")
        info.install.append("composer install --no-interaction --prefer-dist")
    require = {**comp.get("require", {}), **comp.get("require-dev", {})}
    laravel = (ws / "artisan").exists() or "laravel/framework" in require
    if laravel:
        info.types.append("laravel")
    if (ws / "wp-config.php").exists() or (ws / "wp-content").is_dir() or (ws / "wp-config-sample.php").exists():
        info.types.append("wordpress")
    phpunit_cfg = (ws / "phpunit.xml").exists() or (ws / "phpunit.xml.dist").exists()
    php_tests = _any_file(ws, lambda p: p.suffix == ".php" and p.name.endswith("Test.php"))
    if laravel and (phpunit_cfg or php_tests):
        info.has_tests = True
        info.test.append("php artisan test")
    elif phpunit_cfg or php_tests:
        info.has_tests = True
        if (ws / "vendor" / "bin" / "phpunit").exists():
            info.test.append("./vendor/bin/phpunit")
        elif "phpunit/phpunit" in require:
            info.test.append("composer exec phpunit")
        elif "test" in (comp.get("scripts") or {}):
            info.test.append("composer test")
    elif "test" in (comp.get("scripts") or {}):
        info.has_tests = True
        info.test.append("composer test")
    if "lint" in (comp.get("scripts") or {}):
        info.lint.append("composer lint")
    if "phpstan/phpstan" in require:
        info.typecheck.append("composer exec phpstan analyse")


def _detect_java(ws: Path, info: ProjectInfo) -> None:
    if (ws / "pom.xml").exists():
        info.types.append("java-maven")
        mvn = "./mvnw" if (ws / "mvnw").exists() else "mvn"
        info.test.append(f"{mvn} -q test")
        info.build.append(f"{mvn} -q package -DskipTests")
        info.has_tests = (ws / "src" / "test").is_dir()
    elif (ws / "build.gradle").exists() or (ws / "build.gradle.kts").exists():
        info.types.append("java-gradle")
        gradle = "./gradlew" if (ws / "gradlew").exists() else "gradle"
        info.test.append(f"{gradle} test")
        info.build.append(f"{gradle} build -x test")
        info.has_tests = (ws / "src" / "test").is_dir()


def detect(ws: Path, cfg: dict) -> ProjectInfo:
    info = ProjectInfo()
    for fn in (_detect_python, _detect_node, _detect_php, _detect_java):
        fn(ws, info)
    ov = cfg.get("overrides", {})
    for key in ("install", "test", "lint", "typecheck"):
        if ov.get(key):
            setattr(info, key, list(ov[key]))
            if key == "test":
                info.has_tests = True
    if cfg.get("build_commands"):
        info.build = list(cfg["build_commands"])
    return info


def manifest_fingerprint(ws: Path) -> str:
    parts = []
    for name in MANIFESTS:
        p = ws / name
        if p.exists():
            parts.append(f"{name}:{_read(p)}")
    return "\n".join(parts)


TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|Test\.php$",
    re.I,
)


def is_test_path(rel: str) -> bool:
    return bool(TEST_PATH_RE.search(rel))
