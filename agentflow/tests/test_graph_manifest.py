"""graph_manifest: dependencies from pyproject.toml, requirements*.txt and package.json, and facts `check_facts` always accepts."""

from __future__ import annotations

import json

from agentflow import graph_model as gm
from agentflow.graph_manifest import extract


def _roundtrip(path: str, data: bytes) -> gm.Facts:
    """The facts for `data`, proven to pass `check_facts` unchanged."""
    facts = extract(path, data)
    checked = gm.check_facts(gm.to_json(facts), "manifest", data.count(b"\n") + 1)
    assert checked == facts
    return facts


def test_manifests():
    py = _roundtrip(
        "pyproject.toml",
        b'[project]\nname = "My_Pkg"\ndependencies = ["PyYAML>=6", "requests[socks] ; python_version<\\"4\\""]\n[project.optional-dependencies]\ndev = ["pytest"]\n',
    )
    assert (py.package, py.ecosystem) == ("my-pkg", "pypi")
    assert [(d.name, d.group) for d in py.deps] == [("pyyaml", "runtime"), ("requests", "runtime"), ("pytest", "optional")]
    js = _roundtrip("web/package.json", b'{"name": "@acme/web", "dependencies": {"react": "^19"}, "devDependencies": {"vitest": "1"}}')
    assert (js.package, js.ecosystem) == ("@acme/web", "npm")
    assert [(d.name, d.spec, d.group) for d in js.deps] == [("react", "^19", "runtime"), ("vitest", "1", "dev")]
    req = _roundtrip("requirements-dev.txt", b"# c\n-r base.txt\nruff==0.17.4\n\n")
    assert [(d.name, d.spec) for d in req.deps] == [("ruff", "==0.17.4")]
    assert (req.package, req.ecosystem) == ("", "pypi")


def test_pyproject_requirements():
    data = (
        b'[project]\nname = "x"\ndependencies = [\n "Zope.Interface>=5,<6", "a_b__c ==1.0", "pkg (>=1.0)", "bare", "url @ https://h/p.whl",\n'
        b' "env>=1; python_version < \\"3.12\\" and extra == \\"x\\"", "spaced >= 1 , < 2",\n]\n'
    )
    f = _roundtrip("pyproject.toml", data)
    assert [(d.name, d.spec) for d in f.deps] == [
        ("zope-interface", ">=5,<6"), ("a-b-c", "==1.0"), ("pkg", ">=1.0"), ("bare", ""), ("url", "@ https://h/p.whl"), ("env", ">=1"), ("spaced", ">= 1 , < 2"),
    ]  # fmt: skip
    assert f.notes == ()


def test_pyproject_without_project_table_and_name_cases():
    f = _roundtrip("pyproject.toml", b'[tool.x]\na = 1\n')
    assert f == gm.Facts(lang="manifest", ecosystem="pypi", notes=("pyproject.toml has no [project] table",))
    g = _roundtrip("pyproject.toml", b'[project]\ndynamic = ["version"]\n')
    assert (g.package, g.deps, g.notes) == ("", (), ())
    h = _roundtrip("pyproject.toml", "[project]\nname = \"café\"\n".encode())
    assert h.package == "" and len(h.notes) == 1
    assert _roundtrip("pyproject.toml", b"\xef\xbb\xbf[project]\nname = \"a.b\"\n").package == "a-b"


def test_dependency_name_with_a_space_is_dropped():
    f = _roundtrip("pyproject.toml", b'[project]\ndependencies = ["bad name>=1", "ok>=1", "-e .", "", 3, "caf\\u00e9>=1", "x[unclosed"]\n')
    assert [d.name for d in f.deps] == ["ok"]
    assert len(f.notes) == 1 and f.notes[0].startswith("dropped 6 dependencies")
    g = _roundtrip("package.json", b'{"dependencies": {"bad name": "1", "good": "1", "num": 3, "caf\\u00e9": "1"}}')
    assert [d.name for d in g.deps] == ["good"] and g.notes[0].startswith("dropped 3 dependencies")


def test_malformed_manifests_give_empty_with_a_note():
    for path, data in (
        ("package.json", b'["a", "b"]'),
        ("package.json", b"not json"),
        ("package.json", b'{"dependencies": "react"}'),
        ("package.json", b'{"name": "x", "devDependencies": ["a"]}'),
        ("package.json", b"\xff"),
        ("package.json", b"[" * 100_000),
        ("pyproject.toml", b"[project\nname = 1"),
        ("pyproject.toml", b'[project]\ndependencies = "requests"\n'),
        ("pyproject.toml", b"[project.optional-dependencies]\ndev = 1\n"),
        ("pyproject.toml", b"project = 1\n"),
        ("pyproject.toml", b"\xff"),
        ("pyproject.toml", b"a = " + b"[" * 100_000),
        ("setup.cfg", b"[metadata]\n"),
    ):
        f = _roundtrip(path, data)
        assert f.deps == () and f.package == "" and len(f.notes) == 1 and f.notes[0], (path, data[:20])
        assert f == gm.empty("manifest", f.notes[0])


def test_requirements_txt():
    data = (
        b"# comment\n-r base.txt\n--index-url https://x\n-e .\n\n  Django_REST.framework >= 3.0  # why\nrequests[socks]==2.0 ; python_version<'4'\n"
        b"flask==1.0 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def\nlast\n./local\ngit+https://h/x.git#egg=x\n"
    )
    f = _roundtrip("requirements.txt", data)
    assert [(d.name, d.spec, d.group) for d in f.deps] == [
        ("django-rest-framework", ">= 3.0", "runtime"), ("requests", "==2.0", "runtime"), ("flask", "==1.0", "runtime"), ("last", "", "runtime"),
    ]  # fmt: skip
    assert len(f.notes) == 1 and f.notes[0].startswith("dropped 2 dependencies")
    assert [d.group for d in _roundtrip("sub/requirements-test.txt", b"pytest\n").deps] == ["dev"]
    assert _roundtrip("requirements.txt", b"a==1\r\nb==2\r\n").deps == (gm.Dep("a", "==1", "runtime"), gm.Dep("b", "==2", "runtime"))
    assert _roundtrip("requirements.txt", b"").deps == ()


def test_package_json_groups():
    f = _roundtrip(
        "package.json",
        json.dumps(
            {
                "name": "no spaces here",
                "dependencies": {"a": "1"},
                "devDependencies": {"b": "2"},
                "optionalDependencies": {"c": "3"},
                "peerDependencies": {"d": "4", "@s/e": "workspace:*"},
            }
        ).encode(),
    )
    assert f.package == "" and len(f.notes) == 1
    assert [(d.name, d.group) for d in f.deps] == [("a", "runtime"), ("b", "dev"), ("c", "optional"), ("d", "optional"), ("@s/e", "optional")]
    assert _roundtrip("package.json", b"{}") == gm.Facts(lang="manifest", ecosystem="npm")


def test_many_dependencies_are_cut():
    f = _roundtrip("requirements.txt", b"".join(b"p%d==1\n" % i for i in range(3000)))
    assert len(f.deps) == gm.MAX_DEPS and f.notes == (f"cut dependencies to {gm.MAX_DEPS}",)
    g = _roundtrip("package.json", json.dumps({"dependencies": {f"p{i}": "1" for i in range(3000)}}).encode())
    assert len(g.deps) == gm.MAX_DEPS and len(g.notes) == 1


def test_long_specs_and_hostile_lines():
    f = _roundtrip("requirements.txt", b"a==" + b"1" * 5000 + b"\n")
    assert len(f.deps[0].spec) == gm.NAME_MAX
    assert _roundtrip("requirements.txt", b"a" * 500 + b"==1\n").deps == ()  # a name past 200 characters
    for line in (b"a" + b" " * 300_000 + b"x", b"a[" * 100_000, b" --" * 100_000 + b"a", b"\\\n" * 100_000):
        _roundtrip("requirements.txt", line + b"\n")


def test_url_credentials_never_reach_the_facts():
    py = _roundtrip(
        "pyproject.toml",
        b'[project]\ndependencies = ["a @ git+https://user:SECRETTOKEN@host.example/org/a.git@v1.0#egg=a", "b @ https://SECRETTOKEN@host.example/b.whl", "c @ https://host.example/c@v2"]\n',
    )
    assert [(d.name, d.spec) for d in py.deps] == [
        ("a", "@ git+https://host.example/org/a.git@v1.0#egg=a"), ("b", "@ https://host.example/b.whl"), ("c", "@ https://host.example/c@v2"),
    ]  # fmt: skip
    req = _roundtrip("requirements.txt", b"a @ git+https://user:SECRETTOKEN@host.example/a.git\nb @ https://SECRETTOKEN@host.example/b.whl\n")
    assert [d.spec for d in req.deps] == ["@ git+https://host.example/a.git", "@ https://host.example/b.whl"]
    js = _roundtrip(
        "package.json",
        b'{"name": "@acme/web", "dependencies": {"a": "git+https://user:SECRETTOKEN@host.example/a.git#v1.0", "b": "https://SECRETTOKEN@host.example/b.tgz", "@acme/c": "^1"}}',
    )
    assert [(d.name, d.spec) for d in js.deps] == [("a", "git+https://host.example/a.git#v1.0"), ("b", "https://host.example/b.tgz"), ("@acme/c", "^1")]
    assert js.package == "@acme/web"
    for facts in (py, req, js):
        assert "SECRETTOKEN" not in json.dumps(gm.to_json(facts))
