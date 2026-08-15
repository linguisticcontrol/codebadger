"""Opt-in checks against the real Go, C/C++, and C# Joern frontends."""

import io
import os
import re
import tarfile
import uuid

import docker
import pytest

from src.tools.core_tools import _build_frontend_exclude_regex


FRONTENDS = (
    ("go", "go", "/opt/joern/joern-cli/gosrc2cpg"),
    ("cpp", "cpp", "/opt/joern/joern-cli/c2cpg.sh"),
    ("csharp", "cs", "/opt/joern/joern-cli/csharpsrc2cpg"),
)

METHODS = {
    "internal/kept": "ScopedInternalKept",
    "tools/kept": "ScopedToolsKept",
    "root": "ScopedRootDropped",
    "other/unrelated": "ScopedUnrelatedDropped",
    "pkg/internal/lookalike": "ScopedLookalikeDropped",
    "tools/generated/excluded": "ScopedExplicitlyDropped",
}
EXPECTED_METHODS = {"ScopedInternalKept", "ScopedToolsKept"}
EXCLUDED_METHODS = set(METHODS.values()) - EXPECTED_METHODS
QUERY = (
    "@main def main(cpgFile: String, projectName: String) = {\n"
    "  importCpg(cpgFile, projectName)\n"
    '  println("SCOPED_METHODS=" + '
    "cpg.method.isExternal(false).name.l.sorted.mkString(\",\"))\n"
    "}\n"
)


def _method_source(language, name):
    if language == "go":
        return f"package fixture\n\nfunc {name}() {{}}\n"
    if language == "cpp":
        return f"void {name}() {{}}\n"
    return (
        f"public static class Holder{name} "
        f"{{ public static void {name}() {{ }} }}\n"
    )


def _fixture_files(language, extension):
    files = {
        f"{path}.{extension}": _method_source(language, method)
        for path, method in METHODS.items()
    }
    if language == "go":
        files["go.mod"] = "module codebadger.test/pathscope\n\ngo 1.22\n"
    elif language == "csharp":
        files["Fixture.csproj"] = (
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
            "<TargetFramework>net8.0</TargetFramework>"
            "</PropertyGroup></Project>\n"
        )
    return files


def _tar_bytes(files):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
    return payload.getvalue()


def _exec(container, command, *, environment=None, workdir=None):
    result = container.exec_run(
        command, environment=environment, workdir=workdir
    )
    output = result.output.decode("utf-8", "replace")
    assert result.exit_code == 0, (
        f"command failed ({result.exit_code}): {' '.join(command)}\n{output}"
    )
    return output


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.timeout(360)
def test_exact_frontend_root_scoping_across_racegame_frontends():
    if os.getenv("CODEBADGER_REAL_FRONTEND_TESTS") != "1":
        pytest.skip("set CODEBADGER_REAL_FRONTEND_TESTS=1 to run real frontends")

    client = docker.from_env()
    container = client.containers.get(
        os.getenv("JOERN_CONTAINER_NAME", "codebadger-joern-server")
    )
    java_opts = {"JAVA_OPTS": "-Xmx2G -XX:+UseG1GC -Dfile.encoding=UTF-8"}

    try:
        for language, extension, frontend in FRONTENDS:
            token = uuid.uuid4().hex[:16]
            source_root = f"/playground/codebases/{token}"
            cpg_path = f"/playground/cpgs/{token}.bin"
            query_path = f"{source_root}/methods.sc"

            try:
                _exec(container, ["mkdir", "-p", source_root])
                assert container.put_archive(
                    source_root, _tar_bytes(_fixture_files(language, extension))
                )

                exclude_regex = _build_frontend_exclude_regex(
                    language,
                    include_globs=["internal/**", "tools/**"],
                    exclude_globs=["tools/generated/**"],
                    frontend_input_root=source_root,
                )
                _exec(
                    container,
                    [
                        frontend,
                        source_root,
                        "-o",
                        cpg_path,
                        "--exclude-regex",
                        exclude_regex,
                    ],
                    environment=java_opts,
                )

                assert container.put_archive(
                    source_root, _tar_bytes({"methods.sc": QUERY})
                )
                output = _exec(
                    container,
                    [
                        "/opt/joern/joern-cli/joern",
                        "--script",
                        query_path,
                        "--param",
                        f"cpgFile={cpg_path}",
                        "--param",
                        f"projectName={token}",
                    ],
                    environment=java_opts,
                    workdir=source_root,
                )
                match = re.search(r"SCOPED_METHODS=([^\r\n]*)", output)
                assert match, output
                methods = set(filter(None, match.group(1).split(",")))
                assert EXPECTED_METHODS <= methods
                assert EXCLUDED_METHODS.isdisjoint(methods)
            finally:
                container.exec_run(["rm", "-rf", source_root, cpg_path])
    finally:
        client.close()
