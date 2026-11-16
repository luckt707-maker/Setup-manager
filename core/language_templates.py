"""Language templates for project detection, scaffolding, and setup.

Each language template describes everything SetupManager needs to recognize,
bootstrap, and maintain a project: file markers, scaffold files, setup
commands, quality-of-life extensions, and runtime requirements.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional


class LanguageTemplate:
    """A single language template with all metadata needed for setup."""

    def __init__(
        self,
        key: str,
        display_name: str,
        markers: List[str],
        extensions: List[str],
        runtime_check: str,
        runtime_url: str,
        setup_cmd: str,
        scaffold: Optional[Dict[str, str]] = None,
        qol: Optional[Dict[str, List[Dict[str, str]]]] = None,
        install_prefixes: Optional[List[str]] = None,
        ignore_patterns: Optional[List[str]] = None,
    ):
        self.key = key
        self.display_name = display_name
        self.markers = markers
        self.extensions = extensions
        self.runtime_check = runtime_check
        self.runtime_url = runtime_url
        self.setup_cmd = setup_cmd
        self.scaffold = scaffold or {}
        self.qol = qol or {}
        self.install_prefixes = install_prefixes or []
        self.ignore_patterns = ignore_patterns or []


# ---------------------------------------------------------------------------
# Template registry – add a new language here and it is automatically
# available for detection, scaffolding, and QoL extensions.
# ---------------------------------------------------------------------------

TEMPLATES: Dict[str, LanguageTemplate] = {
    "python": LanguageTemplate(
        key="python",
        display_name="Python",
        markers=["requirements.txt", "setup.py", "pyproject.toml", "Pipfile", "*.py"],
        extensions=[".py"],
        runtime_check="python",
        runtime_url="https://www.python.org/downloads/",
        setup_cmd="python -m venv venv",
        install_prefixes=["pip ", "pip3 "],
        scaffold={
            "requirements.txt": "# Add dependencies here\n",
            ".gitignore": "venv/\n__pycache__/\n*.pyc\n*.egg-info/\ndist/\nbuild/\n.pytest_cache/\n.mypy_cache/\n",
            "README.md": "# Project\n\nPython project.\n",
        },
        qol={
            "linters": [
                {"name": "pylint", "cmd": "pip install pylint", "run": "pylint .", "desc": "Code quality checker"},
                {"name": "flake8", "cmd": "pip install flake8", "run": "flake8 .", "desc": "Style guide enforcement"},
                {"name": "black", "cmd": "pip install black", "run": "black .", "desc": "Code formatter"},
            ],
            "type_checkers": [
                {"name": "mypy", "cmd": "pip install mypy", "run": "mypy .", "desc": "Static type checker"},
            ],
            "testers": [
                {"name": "pytest", "cmd": "pip install pytest pytest-cov", "run": "pytest", "desc": "Testing framework"},
                {"name": "unittest", "cmd": "python -m unittest", "run": "python -m unittest discover", "desc": "Built-in testing"},
            ],
            "security": [
                {"name": "bandit", "cmd": "pip install bandit", "run": "bandit -r .", "desc": "Security issue finder"},
            ],
            "profilers": [
                {"name": "cProfile", "cmd": "python -m cProfile", "run": "python -m cProfile -s cumtime your_script.py", "desc": "Performance profiler"},
            ],
            "debuggers": [
                {"name": "pdb", "cmd": "python -m pdb", "run": "python -m pdb your_script.py", "desc": "Python debugger"},
                {"name": "ipdb", "cmd": "pip install ipdb", "run": "ipdb your_script.py", "desc": "Enhanced debugger"},
            ],
        },
    ),
    "nodejs": LanguageTemplate(
        key="nodejs",
        display_name="Node.js",
        markers=["package.json", "yarn.lock", "pnpm-lock.yaml", "*.js", "*.ts"],
        extensions=[".js", ".ts", ".jsx", ".tsx"],
        runtime_check="node",
        runtime_url="https://nodejs.org/en/download",
        setup_cmd="npm install",
        install_prefixes=["npm install ", "npm i ", "npx "],
        scaffold={
            "package.json": '{\n  "name": "project",\n  "version": "1.0.0",\n  "description": "",\n  "main": "index.js",\n  "scripts": {\n    "start": "node index.js",\n    "test": "echo \\"Error: no test specified\\" && exit 1"\n  },\n  "keywords": [],\n  "author": "",\n  "license": "ISC"\n}\n',
            ".gitignore": "node_modules/\nnpm-debug.log*\nyarn-debug.log*\nyarn-error.log*\n.DS_Store\ndist/\nbuild/\n.env\n",
            "README.md": "# Project\n\nNode.js project.\n",
        },
        qol={
            "linters": [
                {"name": "eslint", "cmd": "npm install --save-dev eslint", "run": "npx eslint .", "desc": "JS linter"},
                {"name": "prettier", "cmd": "npm install --save-dev prettier", "run": "npx prettier --write .", "desc": "Code formatter"},
            ],
            "type_checkers": [
                {"name": "TypeScript", "cmd": "npm install --save-dev typescript", "run": "npx tsc --noEmit", "desc": "Type checking"},
            ],
            "testers": [
                {"name": "Jest", "cmd": "npm install --save-dev jest", "run": "npm test", "desc": "Testing framework"},
                {"name": "Mocha", "cmd": "npm install --save-dev mocha chai", "run": "npx mocha", "desc": "Testing framework"},
            ],
            "security": [
                {"name": "npm audit", "cmd": "npm audit", "run": "npm audit", "desc": "Dependency vulnerabilities"},
            ],
            "profilers": [
                {"name": "Node profiler", "cmd": "node --prof", "run": "node --prof your_script.js", "desc": "Performance profiler"},
            ],
            "debuggers": [
                {"name": "Node debugger", "cmd": "node --inspect", "run": "node --inspect your_script.js", "desc": "Node debugger"},
            ],
        },
    ),
    "dotnet": LanguageTemplate(
        key="dotnet",
        display_name=".NET",
        markers=["*.csproj", "*.sln", "*.cs"],
        extensions=[".cs", ".csproj", ".sln"],
        runtime_check="dotnet",
        runtime_url="https://dotnet.microsoft.com/download",
        setup_cmd="dotnet restore",
        install_prefixes=["dotnet add package "],
        scaffold={
            "Project.csproj": '<Project Sdk="Microsoft.NET.Sdk">\n\n  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n    <TargetFramework>net8.0</TargetFramework>\n    <ImplicitUsings>enable</ImplicitUsings>\n    <Nullable>enable</Nullable>\n  </PropertyGroup>\n\n</Project>\n',
            "Program.cs": 'Console.WriteLine("Hello, World!");\n',
            ".gitignore": "bin/\nobj/\n*.user\n*.suo\n.vs/\n",
            "README.md": "# Project\n\n.NET project.\n",
        },
        qol={
            "linters": [
                {"name": "StyleCop", "cmd": "dotnet add package StyleCop.Analyzers", "run": "dotnet build", "desc": "Code style analyzer"},
            ],
            "type_checkers": [
                {"name": "Roslyn", "cmd": "dotnet build", "run": "dotnet build", "desc": "Type checking"},
            ],
            "testers": [
                {"name": "xUnit", "cmd": "dotnet add package xunit", "run": "dotnet test", "desc": "Testing framework"},
                {"name": "NUnit", "cmd": "dotnet add package NUnit", "run": "dotnet test", "desc": "Testing framework"},
            ],
            "security": [
                {"name": "Security Analyzer", "cmd": "dotnet add package Security.Analyzers", "run": "dotnet build", "desc": "Security checks"},
            ],
            "debuggers": [
                {"name": "Visual Studio Debugger", "cmd": "dotnet build -c Debug", "run": "dotnet run --configuration Debug", "desc": ".NET debugger"},
            ],
        },
    ),
    "java": LanguageTemplate(
        key="java",
        display_name="Java",
        markers=["pom.xml", "build.gradle", "build.gradle.kts", "*.java", "settings.gradle"],
        extensions=[".java", ".class"],
        runtime_check="java",
        runtime_url="https://www.oracle.com/java/technologies/downloads/",
        setup_cmd="mvn clean install",
        install_prefixes=["mvn ", "gradle ", "./gradle "],
        scaffold={
            "pom.xml": '<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0"\n         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">\n    <modelVersion>4.0.0</modelVersion>\n\n    <groupId>com.example</groupId>\n    <artifactId>project</artifactId>\n    <version>1.0-SNAPSHOT</version>\n    <packaging>jar</packaging>\n\n    <properties>\n        <maven.compiler.source>17</maven.compiler.source>\n        <maven.compiler.target>17</maven.compiler.target>\n        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n    </properties>\n\n    <dependencies>\n        <dependency>\n            <groupId>junit</groupId>\n            <artifactId>junit</artifactId>\n            <version>4.13.2</version>\n            <scope>test</scope>\n        </dependency>\n    </dependencies>\n</project>\n',
            ".gitignore": "target/\n*.class\n*.log\n.mvn/\n.idea/\n*.iml\n",
            "README.md": "# Project\n\nJava / Maven project.\n",
        },
        qol={
            "linters": [
                {"name": "Checkstyle", "cmd": "mvn checkstyle:check", "run": "mvn checkstyle:check", "desc": "Code style checker"},
                {"name": "SpotBugs", "cmd": "mvn spotbugs:check", "run": "mvn spotbugs:check", "desc": "Bug detector"},
            ],
            "testers": [
                {"name": "JUnit", "cmd": "mvn test", "run": "mvn test", "desc": "Testing framework"},
            ],
            "security": [
                {"name": "OWASP Dependency Check", "cmd": "mvn org.owasp:dependency-check-maven:check", "run": "mvn org.owasp:dependency-check-maven:check", "desc": "Dependency vulnerabilities"},
            ],
        },
        ignore_patterns=["target/", "*.class", ".mvn/"],
    ),
    "go": LanguageTemplate(
        key="go",
        display_name="Go",
        markers=["go.mod", "go.sum", "*.go"],
        extensions=[".go"],
        runtime_check="go",
        runtime_url="https://go.dev/dl/",
        setup_cmd="go mod tidy",
        install_prefixes=["go get ", "go install "],
        scaffold={
            "go.mod": "module project\n\ngo 1.21\n",
            "main.go": 'package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("Hello, World!")\n}\n',
            ".gitignore": "vendor/\n*.exe\n*.test\n*.out\n",
            "README.md": "# Project\n\nGo project.\n",
        },
        qol={
            "linters": [
                {"name": "golangci-lint", "cmd": "go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest", "run": "golangci-lint run", "desc": "Fast linter"},
                {"name": "golint", "cmd": "go install golang.org/x/lint/golint@latest", "run": "golint ./...", "desc": "Style linter"},
            ],
            "formatters": [
                {"name": "gofmt", "cmd": "go fmt ./...", "run": "go fmt ./...", "desc": "Built-in formatter"},
            ],
            "testers": [
                {"name": "go test", "cmd": "go test ./...", "run": "go test ./...", "desc": "Built-in testing"},
            ],
            "security": [
                {"name": "govulncheck", "cmd": "go install golang.org/x/vuln/cmd/govulncheck@latest", "run": "govulncheck ./...", "desc": "Vulnerability checker"},
            ],
        },
    ),
    "rust": LanguageTemplate(
        key="rust",
        display_name="Rust",
        markers=["Cargo.toml", "Cargo.lock", "*.rs"],
        extensions=[".rs"],
        runtime_check="rustc",
        runtime_url="https://www.rust-lang.org/tools/install",
        setup_cmd="cargo build",
        install_prefixes=["cargo install "],
        scaffold={
            "Cargo.toml": '[package]\nname = "project"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
            "src/main.rs": 'fn main() {\n    println!("Hello, world!");\n}\n',
            ".gitignore": "target/\n.idea/\n*.rs.bk\n",
            "README.md": "# Project\n\nRust project.\n",
        },
        qol={
            "linters": [
                {"name": "clippy", "cmd": "cargo clippy", "run": "cargo clippy", "desc": "Linting tool"},
            ],
            "formatters": [
                {"name": "rustfmt", "cmd": "cargo fmt", "run": "cargo fmt", "desc": "Code formatter"},
            ],
            "testers": [
                {"name": "cargo test", "cmd": "cargo test", "run": "cargo test", "desc": "Built-in testing"},
            ],
            "security": [
                {"name": "cargo-audit", "cmd": "cargo install cargo-audit", "run": "cargo audit", "desc": "Security audit"},
            ],
        },
    ),
    "ruby": LanguageTemplate(
        key="ruby",
        display_name="Ruby",
        markers=["Gemfile", "Gemfile.lock", "*.rb", "Rakefile"],
        extensions=[".rb"],
        runtime_check="ruby",
        runtime_url="https://www.ruby-lang.org/en/downloads/",
        setup_cmd="bundle install",
        install_prefixes=["gem install ", "bundle install"],
        scaffold={
            "Gemfile": '# frozen_string_literal: true\n\nsource "https://rubygems.org"\n\n# gem "rails"\n',
            ".gitignore": "/Gemfile.lock\n/.bundle/\n/log/\n/tmp/\n*.gem\n",
            "README.md": "# Project\n\nRuby project.\n",
        },
        qol={
            "linters": [
                {"name": "rubocop", "cmd": "gem install rubocop", "run": "rubocop", "desc": "Code style checker"},
            ],
            "testers": [
                {"name": "rspec", "cmd": "gem install rspec", "run": "rspec", "desc": "Testing framework"},
                {"name": "minitest", "cmd": "gem install minitest", "run": "ruby -Itest -e 'Dir.glob(\"test/**/*_test.rb\").each { |f| require f }'", "desc": "Built-in testing"},
            ],
        },
    ),
    "php": LanguageTemplate(
        key="php",
        display_name="PHP",
        markers=["composer.json", "composer.lock", "*.php", "artisan"],
        extensions=[".php"],
        runtime_check="php",
        runtime_url="https://www.php.net/downloads.php",
        setup_cmd="composer install",
        install_prefixes=["composer require ", "composer install"],
        scaffold={
            "composer.json": '{\n    "name": "vendor/project",\n    "description": "Project",\n    "type": "project",\n    "require": {},\n    "autoload": {\n        "psr-4": {\n            "App\\\\": "src/"\n        }\n    }\n}\n',
            ".gitignore": "vendor/\n.env\n*.log\n",
            "README.md": "# Project\n\nPHP project.\n",
        },
        qol={
            "linters": [
                {"name": "phpstan", "cmd": "composer require --dev phpstan/phpstan", "run": "vendor/bin/phpstan analyse", "desc": "Static analysis"},
                {"name": "phpcs", "cmd": "composer require --dev squizlabs/php_codesniffer", "run": "vendor/bin/phpcs", "desc": "Code style checker"},
            ],
            "testers": [
                {"name": "phpunit", "cmd": "composer require --dev phpunit/phpunit", "run": "vendor/bin/phpunit", "desc": "Testing framework"},
            ],
            "security": [
                {"name": "composer audit", "cmd": "composer audit", "run": "composer audit", "desc": "Dependency audit"},
            ],
        },
    ),
    "typescript": LanguageTemplate(
        key="typescript",
        display_name="TypeScript",
        markers=["tsconfig.json", "*.ts", "*.tsx", "package.json"],
        extensions=[".ts", ".tsx"],
        runtime_check="node",
        runtime_url="https://nodejs.org/en/download",
        setup_cmd="npm install",
        install_prefixes=["npm install ", "npm i ", "npx "],
        scaffold={
            "package.json": '{\n  "name": "project",\n  "version": "1.0.0",\n  "description": "",\n  "main": "dist/index.js",\n  "scripts": {\n    "build": "tsc",\n    "start": "node dist/index.js",\n    "dev": "ts-node src/index.ts",\n    "test": "jest"\n  },\n  "devDependencies": {\n    "typescript": "^5.0.0",\n    "ts-node": "^10.0.0"\n  }\n}\n',
            "tsconfig.json": '{\n  "compilerOptions": {\n    "target": "ES2020",\n    "module": "commonjs",\n    "outDir": "./dist",\n    "rootDir": "./src",\n    "strict": true,\n    "esModuleInterop": true,\n    "skipLibCheck": true,\n    "forceConsistentCasingInFileNames": true\n  },\n  "include": ["src/**/*"],\n  "exclude": ["node_modules", "dist"]\n}\n',
            "src/index.ts": 'console.log("Hello, TypeScript!");\n',
            ".gitignore": "node_modules/\ndist/\n.env\n*.tsbuildinfo\n",
            "README.md": "# Project\n\nTypeScript project.\n",
        },
        qol={
            "linters": [
                {"name": "eslint", "cmd": "npm install --save-dev eslint @typescript-eslint/parser @typescript-eslint/eslint-plugin", "run": "npx eslint .", "desc": "TS linter"},
                {"name": "prettier", "cmd": "npm install --save-dev prettier", "run": "npx prettier --write .", "desc": "Code formatter"},
            ],
            "type_checkers": [
                {"name": "tsc", "cmd": "npm install --save-dev typescript", "run": "npx tsc --noEmit", "desc": "TypeScript compiler"},
            ],
            "testers": [
                {"name": "Jest", "cmd": "npm install --save-dev jest ts-jest @types/jest", "run": "npx jest", "desc": "Testing framework"},
            ],
        },
    ),
    "csharp": LanguageTemplate(
        key="csharp",
        display_name="C#",
        markers=["*.csproj", "*.sln", "*.cs"],
        extensions=[".cs", ".csproj", ".sln"],
        runtime_check="dotnet",
        runtime_url="https://dotnet.microsoft.com/download",
        setup_cmd="dotnet restore",
        install_prefixes=["dotnet add package "],
        scaffold={
            "Project.csproj": '<Project Sdk="Microsoft.NET.Sdk">\n\n  <PropertyGroup>\n    <OutputType>Exe</OutputType>\n    <TargetFramework>net8.0</TargetFramework>\n    <ImplicitUsings>enable</ImplicitUsings>\n    <Nullable>enable</Nullable>\n  </PropertyGroup>\n\n</Project>\n',
            "Program.cs": 'Console.WriteLine("Hello, World!");\n',
            ".gitignore": "bin/\nobj/\n*.user\n*.suo\n.vs/\n",
            "README.md": "# Project\n\nC# project.\n",
        },
        qol={
            "linters": [
                {"name": "StyleCop", "cmd": "dotnet add package StyleCop.Analyzers", "run": "dotnet build", "desc": "Code style analyzer"},
            ],
            "testers": [
                {"name": "xUnit", "cmd": "dotnet add package xunit", "run": "dotnet test", "desc": "Testing framework"},
            ],
        },
    ),
    "kotlin": LanguageTemplate(
        key="kotlin",
        display_name="Kotlin",
        markers=["build.gradle.kts", "build.gradle", "pom.xml", "*.kt"],
        extensions=[".kt", ".kts"],
        runtime_check="java",
        runtime_url="https://www.oracle.com/java/technologies/downloads/",
        setup_cmd="./gradlew build",
        install_prefixes=["./gradlew ", "gradle ", "mvn "],
        scaffold={
            "build.gradle.kts": 'plugins {\n    kotlin("jvm") version "1.9.0"\n}\n\ngroup = "com.example"\nversion = "1.0-SNAPSHOT"\n\nrepositories {\n    mavenCentral()\n}\n\ndependencies {\n    testImplementation(kotlin("test"))\n}\n\ntasks.test {\n    useJUnitPlatform()\n}\n',
            ".gitignore": "build/\n.gradle/\n.idea/\n*.iml\n",
            "README.md": "# Project\n\nKotlin project.\n",
        },
        qol={
            "linters": [
                {"name": "ktlint", "cmd": "./gradlew ktlintCheck", "run": "./gradlew ktlintCheck", "desc": "Kotlin linter"},
            ],
            "testers": [
                {"name": "JUnit", "cmd": "./gradlew test", "run": "./gradlew test", "desc": "Testing framework"},
            ],
        },
    ),
    "swift": LanguageTemplate(
        key="swift",
        display_name="Swift",
        markers=["Package.swift", "*.swift", "Cartfile"],
        extensions=[".swift"],
        runtime_check="swift",
        runtime_url="https://www.swift.org/download/",
        setup_cmd="swift build",
        install_prefixes=["swift package update"],
        scaffold={
            "Package.swift": '// swift-tools-version: 5.9\nimport PackageDescription\n\nlet package = Package(\n    name: "Project",\n    products: [\n        .executable(name: "Project", targets: ["Project"]),\n    ],\n    targets: [\n        .executableTarget(name: "Project"),\n    ],\n)\n',
            "Sources/Project/main.swift": 'print("Hello, world!")\n',
            ".gitignore": ".build/\nPackages/\n*.xcodeproj/\n*.xcworkspace/\n",
            "README.md": "# Project\n\nSwift project.\n",
        },
        qol={
            "linters": [
                {"name": "swiftlint", "cmd": "brew install swiftlint", "run": "swiftlint", "desc": "Swift linter"},
            ],
            "testers": [
                {"name": "swift test", "cmd": "swift test", "run": "swift test", "desc": "Built-in testing"},
            ],
        },
    ),
    "scala": LanguageTemplate(
        key="scala",
        display_name="Scala",
        markers=["build.sbt", "*.scala", "project/build.properties"],
        extensions=[".scala", ".sbt"],
        runtime_check="java",
        runtime_url="https://www.scala-lang.org/download/",
        setup_cmd="sbt compile",
        install_prefixes=["sbt "],
        scaffold={
            "build.sbt": 'name := "Project"\nversion := "0.1"\nscalaVersion := "2.13.12"\n\nlibraryDependencies += "org.scalatest" %% "scalatest" % "3.2.17" % Test\n',
            ".gitignore": "target/\nproject/target/\nproject/project/\n*.class\n",
            "README.md": "# Project\n\nScala project.\n",
        },
        qol={
            "linters": [
                {"name": "scalafmt", "cmd": "sbt scalafmt", "run": "sbt scalafmtCheckAll", "desc": "Code formatter"},
            ],
            "testers": [
                {"name": "sbt test", "cmd": "sbt test", "run": "sbt test", "desc": "Built-in testing"},
            ],
        },
    ),
    "make": LanguageTemplate(
        key="make",
        display_name="Make",
        markers=["Makefile", "makefile", "CMakeLists.txt"],
        extensions=[".mk"],
        runtime_check="make",
        runtime_url="https://gnuwin32.sourceforge.net/packages/make.htm",
        setup_cmd="make",
        scaffold={
            "Makefile": '.PHONY: all clean\n\nall:\n\t@echo "Build complete"\n\nclean:\n\t@echo "Clean complete"\n',
            ".gitignore": "build/\ndist/\n",
        },
    ),
    "git": LanguageTemplate(
        key="git",
        display_name="Git",
        markers=[".git"],
        extensions=[],
        runtime_check="git",
        runtime_url="https://git-scm.com/downloads",
        setup_cmd="git init",
        scaffold={
            ".gitignore": "# OS files\n.DS_Store\nThumbs.db\n\n# IDE\n.idea/\n.vscode/\n*.swp\n",
        },
    ),
    "docker": LanguageTemplate(
        key="docker",
        display_name="Docker",
        markers=["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
        extensions=[".dockerfile"],
        runtime_check="docker",
        runtime_url="https://www.docker.com/products/docker-desktop",
        setup_cmd="docker compose up -d",
        scaffold={
            "docker-compose.yml": 'version: "3.8"\nservices:\n  app:\n    build: .\n    ports:\n      - "8080:8080"\n',
            "Dockerfile": 'FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nCMD ["python", "app.py"]\n',
            ".gitignore": ".docker/\n",
        },
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_template(key: str) -> Optional[LanguageTemplate]:
    """Look up a template by key."""
    return TEMPLATES.get(key)


def list_templates() -> Dict[str, LanguageTemplate]:
    """Return all registered templates."""
    return dict(TEMPLATES)


def get_all_markers() -> Dict[str, List[str]]:
    """Return {key: markers} for all templates – useful for detector."""
    return {key: tmpl.markers for key, tmpl in TEMPLATES.items()}


def get_all_qol() -> Dict[str, Dict[str, List[Dict[str, str]]]]:
    """Return {key: qol} for all templates – useful for extensions registry."""
    return {key: tmpl.qol for key, tmpl in TEMPLATES.items() if tmpl.qol}


def get_all_install_prefixes() -> Dict[str, List[str]]:
    """Return {key: install_prefixes} for all templates."""
    return {key: tmpl.install_prefixes for key, tmpl in TEMPLATES.items() if tmpl.install_prefixes}
