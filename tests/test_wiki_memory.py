from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wiki_memory.capture import _frontmatter, _parse_frontmatter, canonicalize_url, capture_item, social_import
from wiki_memory.config import load_data
from wiki_memory.dependencies import app_install_command, dependency_report, version_tuple
from wiki_memory.installation import prepare_installation
from wiki_memory.layout import create_vault, init_memory
from wiki_memory.quality import doctor_memory, lint_memory, maintenance_report, scan_privacy
from wiki_memory.router import recommend_vault
from wiki_memory.search import query_memory
from wiki_memory.sync import configure_syncthing
from wiki_memory.temporal import supersession_proposal, temporal_defaults_from_source, temporal_open_questions


FIXTURES = ROOT / "tests" / "fixtures" / "temporal"


def base_spec() -> dict:
    return {
        "name": "Synthetic Memory",
        "language": "fr",
        "client_isolation": False,
        "sync_enabled": True,
        "versioning_confirmed": True,
        "connectors": {"reddit": {"enabled": True}},
        "schedules": {},
        "vaults": [
            {
                "slug": "knowledge",
                "title": "Knowledge",
                "purpose": "Research and learning about product strategy",
                "audience": ["owner"],
                "confidentiality": "private",
                "lifecycle": "ongoing",
                "routing": {"include": ["research", "product"], "exclude": ["client"], "keywords": ["strategy"]},
            }
        ],
    }


class WikiMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.previous_runtime = os.environ.get("WIKI_MEMORY_RUNTIME")
        os.environ["WIKI_MEMORY_RUNTIME"] = str(Path(self.temp.name) / "runtime")
        self.installation = Path(self.temp.name) / "installation"
        (self.installation / "Agent" / ".codex-plugin").mkdir(parents=True)
        (self.installation / "Agent" / ".codex-plugin" / "plugin.json").write_text(
            '{"name":"wiki-memory"}', encoding="utf-8"
        )
        self.root = self.installation / "Mémoire"
        init_memory(self.root, base_spec())

    def tearDown(self) -> None:
        if self.previous_runtime is None:
            os.environ.pop("WIKI_MEMORY_RUNTIME", None)
        else:
            os.environ["WIKI_MEMORY_RUNTIME"] = self.previous_runtime
        self.temp.cleanup()

    def test_localized_layout_and_config(self) -> None:
        vault = load_data(self.root / "knowledge" / "vault.yaml")
        self.assertEqual(vault["folders"]["outputs"], "03-Synthèses")
        self.assertTrue((self.root / "knowledge" / ".obsidian").is_dir())
        self.assertTrue((self.root / "WIKI.md").is_file())
        self.assertFalse((self.root / "clients").exists())
        self.assertIn("browser-profile", (self.root / ".stignore").read_text(encoding="utf-8"))

    def test_multi_device_sync_is_optional(self) -> None:
        local_installation = Path(self.temp.name) / "local-installation"
        (local_installation / "Agent" / ".codex-plugin").mkdir(parents=True)
        (local_installation / "Agent" / ".codex-plugin" / "plugin.json").write_text(
            '{"name":"wiki-memory"}', encoding="utf-8"
        )
        local_only = local_installation / "Mémoire"
        spec = base_spec()
        spec["sync_enabled"] = False
        spec["versioning_confirmed"] = False
        init_memory(local_only, spec)
        config = load_data(local_only / "memory.config.yaml")
        self.assertFalse(config["sync"]["enabled"])
        self.assertIsNone(config["sync"]["provider"])
        self.assertFalse((local_only / ".stignore").exists())
        self.assertFalse((local_only / "syncthing.ignore.template").exists())
        report = doctor_memory(local_only)
        syncthing = next(item for item in report["checks"] if item["name"] == "dependency:syncthing")
        self.assertTrue(syncthing["ok"])
        self.assertEqual(syncthing["severity"], "warning")

    def test_prepare_installation_creates_agent_and_memory_siblings(self) -> None:
        source = Path(self.temp.name) / "plugin-source"
        (source / ".codex-plugin").mkdir(parents=True)
        (source / ".codex-plugin" / "plugin.json").write_text('{"name":"wiki-memory"}', encoding="utf-8")
        (source / "SKILL.md").write_text("synthetic", encoding="utf-8")
        (source / ".env.local").write_text("must-not-copy", encoding="utf-8")
        installation = Path(self.temp.name) / "prepared-installation"
        result = prepare_installation(installation, source)
        self.assertEqual(Path(result["agent"]), (installation / "Agent").resolve())
        self.assertEqual(Path(result["memory"]), (installation / "Mémoire").resolve())
        self.assertTrue((installation / "Agent" / "SKILL.md").is_file())
        self.assertFalse((installation / "Agent" / ".env.local").exists())
        self.assertEqual({path.name for path in installation.iterdir()}, {"Agent", "Mémoire"})

    def test_syncthing_setup_configures_agent_memory_and_remote_device(self) -> None:
        installation = Path(self.temp.name) / "installation-sync"
        agent = installation / "Agent"
        memory = installation / "Mémoire"
        (agent / ".codex-plugin").mkdir(parents=True)
        (agent / ".codex-plugin" / "plugin.json").write_text('{"name":"wiki-memory"}', encoding="utf-8")
        init_memory(memory, base_spec())
        commands: list[list[str]] = []
        configured_paths: dict[str, str] = {}
        remote_id = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"

        def fake_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            tail = command[3:]
            if tail == ["folders", "list"]:
                output = ""
            elif tail[:2] == ["folders", "add"]:
                configured_paths[tail[tail.index("--id") + 1]] = tail[tail.index("--path") + 1]
                output = ""
            elif tail == ["devices", "list"]:
                output = ""
            elif len(tail) == 4 and tail[0] == "folders" and tail[2:] == ["devices", "list"]:
                output = ""
            elif len(tail) == 3 and tail[0] == "folders" and tail[2] == "dump-json":
                output = json.dumps({"id": tail[1], "path": configured_paths[tail[1]]})
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        result = configure_syncthing(
            memory,
            remote_device_id=remote_id,
            remote_device_name="Portable",
            syncthing_binary="syncthing",
            runner=fake_runner,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["remote_device_configured"])
        folder_adds = [command for command in commands if command[3:5] == ["folders", "add"]]
        self.assertEqual(len(folder_adds), 2)
        self.assertEqual(
            {item["path"] for item in result["folders"].values()},
            {str(agent.resolve()), str((memory / ".wiki-memory" / "data").resolve())},
        )
        self.assertTrue(any(command[3:5] == ["devices", "add"] for command in commands))
        folder_shares = [command for command in commands if command[3] == "folders" and command[5:7] == ["devices", "add"]]
        self.assertEqual(len(folder_shares), 2)
        self.assertTrue((agent / ".stignore").is_file())
        self.assertTrue((memory / ".stignore").is_file())
        self.assertTrue((memory / ".wiki-memory" / "data" / ".stignore").is_file())
        config = load_data(memory / "memory.config.yaml")
        self.assertTrue(config["sync"]["configured_on_this_device"])
        self.assertEqual(config["sync"]["folder_id"], result["folder_id"])
        self.assertEqual(set(config["sync"]["folder_ids"]), {"agent", "memory"})

    def test_router_reuses_or_separates(self) -> None:
        result = recommend_vault(
            self.root,
            {"purpose": "product research strategy", "audience": ["owner"], "confidentiality": "private", "lifecycle": "ongoing"},
        )
        self.assertEqual(result["decision"], "existing_vault")
        separate = recommend_vault(
            self.root,
            {"purpose": "client delivery", "audience": ["customer"], "confidentiality": "restricted", "lifecycle": "project"},
        )
        self.assertEqual(separate["decision"], "new_vault")

    def test_create_independent_vault(self) -> None:
        entry = create_vault(
            self.root,
            {"title": "Synthetic Organization", "purpose": "Isolated engagement", "confidentiality": "restricted"},
        )
        self.assertEqual(entry["slug"], "synthetic-organization")
        registry = load_data(self.root / "vaults.registry.yaml")
        self.assertEqual(len(registry["vaults"]), 2)

    def test_url_canonicalization_dedup_and_revision(self) -> None:
        first = capture_item(
            self.root,
            "knowledge",
            source_type="article",
            source_url="https://Example.com/post/?utm_source=test&x=1#section",
            text="First version",
            connector="web",
        )
        duplicate = capture_item(
            self.root,
            "knowledge",
            source_type="article",
            source_url="https://example.com/post?x=1",
            text="First version",
            connector="web",
        )
        revised = capture_item(
            self.root,
            "knowledge",
            source_type="article",
            source_url="https://example.com/post?x=1",
            text="Second version",
            connector="web",
        )
        self.assertEqual(first["status"], "captured")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(revised["status"], "revised")
        self.assertEqual(canonicalize_url("https://Example.com/a/?utm_medium=x"), "https://example.com/a")
        revisions = list((self.root / "knowledge" / "01-Sources" / "revisions" / first["id"]).glob("*.md"))
        self.assertEqual(len(revisions), 1)
        raw_versions = list((self.root / "knowledge" / "01-Sources" / "raw" / first["id"]).glob("*.json"))
        self.assertEqual(len(raw_versions), 2)

    def test_social_import_and_query_fallback(self) -> None:
        input_path = Path(self.temp.name) / "social.json"
        input_path.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "connector": "reddit",
                            "source_url": "https://reddit.com/r/example/comments/abc/example/",
                            "collection": "A etudier",
                            "title": "Synthetic pricing note",
                            "author": "example-author",
                            "text": "Pricing research suggests a synthetic hypothesis.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = social_import(self.root, "knowledge", input_path)
        self.assertEqual(result[0]["status"], "captured")
        expected = self.root / "knowledge" / "01-Sources" / "items" / "reddit" / "a-etudier" / f"{result[0]['id']}.md"
        self.assertEqual(Path(result[0]["path"]), expected.resolve())
        note = expected.read_text(encoding="utf-8")
        self.assertIn('collection: "A etudier"', note)
        raw_files = list((self.root / "knowledge" / "01-Sources" / "raw" / "reddit" / "a-etudier").rglob("*.json"))
        self.assertEqual(len(raw_files), 1)
        query = query_memory(self.root, "pricing research", 5)
        self.assertIn(query["engine"], {"text-fallback", "qmd"})
        if query["engine"] == "text-fallback":
            self.assertTrue(query["results"])

    def test_lint_finds_broken_link(self) -> None:
        wiki = self.root / "knowledge" / "02-Wiki" / "concept.md"
        wiki.write_text("# Concept\n\n[[missing-note]]\n", encoding="utf-8")
        report = lint_memory(self.root)
        self.assertTrue(any(item["code"] == "broken-wikilink" for item in report["warnings"]))

    def test_lint_accepts_relative_source_link(self) -> None:
        captured = capture_item(self.root, "knowledge", source_type="note", text="Synthetic evidence")
        wiki = self.root / "knowledge" / "02-Wiki" / "evidence.md"
        wiki.write_text(f"# Evidence\n\n[[../01-Sources/items/{captured['id']}]]\n", encoding="utf-8")
        report = lint_memory(self.root)
        self.assertFalse(any(item["code"] == "orphan-source" for item in report["warnings"]))

    def test_replaced_fact_is_queryable_on_both_timelines(self) -> None:
        wiki = self.root / "knowledge" / "02-Wiki"
        for name in ("old-fact.md", "new-fact.md"):
            shutil.copy2(FIXTURES / "replaced" / name, wiki / name)
        sources = self.root / "knowledge" / "01-Sources" / "items"
        for name, published in (("old-source", "2024-01-15"), ("new-source", "2025-03-01")):
            (sources / f"{name}.md").write_text(
                _frontmatter({"published_at": published}) + f"\n\n# {name}\n\nSynthetic source.\n",
                encoding="utf-8",
            )

        current = query_memory(self.root, "Alpine Labs service region", 10)
        current_files = {item.get("file") for item in current["results"]}
        self.assertIn("knowledge/02-Wiki/new-fact.md", current_files)
        self.assertNotIn("knowledge/02-Wiki/old-fact.md", current_files)
        self.assertTrue(any(item["file"].endswith("old-fact.md") for item in current["excluded_stale_facts"]))

        world_then = query_memory(self.root, "Alpine Labs service region", 10, valid_at="2024-06-01")
        world_files = {item.get("file") for item in world_then["results"]}
        self.assertIn("knowledge/02-Wiki/old-fact.md", world_files)
        self.assertNotIn("knowledge/02-Wiki/new-fact.md", world_files)

        system_then = query_memory(self.root, "Alpine Labs service region", 10, system_at="2024-06-01")
        system_files = {item.get("file") for item in system_then["results"]}
        self.assertIn("knowledge/02-Wiki/old-fact.md", system_files)
        self.assertNotIn("knowledge/02-Wiki/new-fact.md", system_files)

        natural = query_memory(
            self.root,
            "Que savait la mémoire sur Alpine Labs au 2024-06-01 ?",
            10,
        )
        self.assertEqual(natural["temporal"]["mode"], "system")

        proposal = supersession_proposal(
            self.root,
            "knowledge/02-Wiki/old-fact.md",
            "knowledge/02-Wiki/new-fact.md",
            observed_at="2025-03-02T10:00:00Z",
        )
        self.assertEqual(proposal["status"], "ready")
        self.assertEqual(proposal["current"], "knowledge/02-Wiki/new-fact.md")

        maintenance = maintenance_report(
            self.root,
            older_than_months=6,
            now="2026-01-01T00:00:00Z",
        )
        self.assertTrue(any(item["path"].endswith("new-fact.md") for item in maintenance["stale_current_facts"]))
        self.assertFalse(maintenance["broken_supersession_chains"])

    def test_source_without_date_creates_a_temporal_open_question(self) -> None:
        metadata, _ = _parse_frontmatter((FIXTURES / "source-without-date.md").read_text(encoding="utf-8"))
        defaults = temporal_defaults_from_source(metadata, recorded_at="2025-05-04T12:30:00Z")
        self.assertIsNone(defaults["valid_from"])
        self.assertEqual(defaults["recorded_at"], "2025-05-04T12:30:00Z")
        questions = temporal_open_questions(metadata)
        self.assertIn("source provides no date", questions[0]["question"])

        captured = capture_item(
            self.root,
            "knowledge",
            source_type="meeting",
            text="Synthetic dated decision.",
            published_at="2025-05-03",
        )
        self.assertEqual(captured["fact_temporal_defaults"]["valid_from"], "2025-05-03")
        self.assertFalse(captured["open_questions"])

        fact = self.root / "knowledge" / "02-Wiki" / "undated-fact.md"
        fact.write_text(
            _frontmatter(defaults)
            + "\n\n# Synthetic packaging choice\n",
            encoding="utf-8",
        )
        maintenance = maintenance_report(self.root, older_than_months=6, now="2025-06-01T00:00:00Z")
        self.assertTrue(any(item["path"].endswith("undated-fact.md") for item in maintenance["missing_valid_from"]))

    def test_ambiguous_temporal_order_is_never_applied(self) -> None:
        wiki = self.root / "knowledge" / "02-Wiki"
        for name in ("fact-a.md", "fact-b.md"):
            shutil.copy2(FIXTURES / "ambiguous" / name, wiki / name)
        before = {name: (wiki / name).read_text(encoding="utf-8") for name in ("fact-a.md", "fact-b.md")}
        report = lint_memory(
            self.root,
            [("knowledge/02-Wiki/fact-a.md", "knowledge/02-Wiki/fact-b.md")],
            observed_at="2025-04-03T08:00:00Z",
        )
        proposal = report["resolution_proposals"][0]
        self.assertEqual(proposal["status"], "ambiguous")
        self.assertEqual(proposal["reason"], "missing-valid-from")
        self.assertEqual(proposal["updates"], {})
        after = {name: (wiki / name).read_text(encoding="utf-8") for name in ("fact-a.md", "fact-b.md")}
        self.assertEqual(before, after)

    def test_maintenance_reports_a_broken_supersession_chain(self) -> None:
        wiki = self.root / "knowledge" / "02-Wiki"
        broken = wiki / "broken-chain.md"
        broken.write_text(
            _frontmatter(
                {
                    "valid_from": "2025-01-01",
                    "valid_until": None,
                    "recorded_at": "2025-01-02T00:00:00Z",
                    "invalidated_at": None,
                    "supersedes": "[[missing-older-fact]]",
                    "superseded_by": None,
                }
            )
            + "\n\n# Synthetic broken chain\n",
            encoding="utf-8",
        )
        report = maintenance_report(self.root, older_than_months=6, now="2025-06-01T00:00:00Z")
        self.assertTrue(
            any(item["code"] == "broken-supersession-target" for item in report["broken_supersession_chains"])
        )

    def test_privacy_scan_detects_absolute_home(self) -> None:
        safe = scan_privacy(ROOT)
        self.assertTrue(safe["ok"], safe["findings"])
        bad = Path(self.temp.name) / "bad.txt"
        bad.write_text("/" + "Users/example/private/file.txt", encoding="utf-8")
        report = scan_privacy(Path(self.temp.name))
        self.assertFalse(report["ok"])

    def test_doctor_reads_versioning_from_config(self) -> None:
        report = doctor_memory(self.root)
        versioning = next(item for item in report["checks"] if item["name"] == "backup-or-versioning")
        self.assertTrue(versioning["ok"])
        dependency_names = {item["name"] for item in report["checks"] if item["name"].startswith("dependency:")}
        self.assertEqual(
            dependency_names,
            {
                "dependency:python",
                "dependency:node",
                "dependency:obsidian",
                "dependency:syncthing",
                "dependency:docling",
                "dependency:qmd",
            },
        )

    def test_cross_platform_app_install_plans(self) -> None:
        available = {
            "brew": "/opt/package-manager/bin/brew",
            "winget": "C:\\PackageManager\\winget.exe",
            "flatpak": "/usr/bin/flatpak",
        }
        which = available.get
        self.assertEqual(
            app_install_command("obsidian", "Darwin", which),
            ["/opt/package-manager/bin/brew", "install", "--cask", "obsidian"],
        )
        self.assertIn("Obsidian.Obsidian", app_install_command("obsidian", "Windows", which) or [])
        self.assertIn("Syncthing.Syncthing", app_install_command("syncthing", "Windows", which) or [])
        self.assertEqual(
            app_install_command("obsidian", "Linux", which),
            ["/usr/bin/flatpak", "install", "--user", "-y", "flathub", "md.obsidian.Obsidian"],
        )
        self.assertGreaterEqual(version_tuple("v24.16.0"), (22,))
        syncthing = next(item for item in dependency_report() if item.name == "syncthing")
        self.assertFalse(syncthing.required)

    def test_onboarding_has_mandatory_dependency_gate(self) -> None:
        skill = (ROOT / "skills/wiki-memory-onboarding/SKILL.md").read_text(encoding="utf-8")
        welcome = "Veux-tu démarrer un échange"
        self.assertIn("First-launch welcome", skill)
        self.assertIn(welcome, skill)
        self.assertIn("Mandatory dependency gate", skill)
        self.assertIn("CE QUE VOUS DONNEZ", skill)
        self.assertIn("01-SOURCES", skill)
        self.assertIn("when a date or proof is missing", skill)
        self.assertIn("<python-launcher> scripts/bootstrap.py --check", skill)
        self.assertIn("<python-launcher> scripts/bootstrap.py --yes --open-links", skill)
        self.assertIn("--yes --with-syncthing --open-links", skill)
        self.assertIn("Souhaites-tu synchroniser ta mémoire sur un autre appareil", skill)
        self.assertIn("If the user declines", skill)
        self.assertIn("Durable installation layout", skill)
        self.assertIn("├── Agent/", skill)
        self.assertIn("└── Mémoire/", skill)
        self.assertIn("prepare-installation <installation-root>", skill)
        self.assertIn("init <installation-root>/Mémoire", skill)
        self.assertIn("two separate Syncthing folders", skill)
        organization_choice = "as-tu déjà une idée de la façon dont ta mémoire devrait être organisée"
        self.assertIn("Choose the starting point", skill)
        self.assertIn(organization_choice, skill)
        self.assertIn("Clearly label assumptions and unknowns", skill)
        self.assertLess(skill.index("First-launch welcome"), skill.index("Mandatory dependency gate"))
        self.assertLess(skill.index("Mandatory dependency gate"), skill.index("Explain the memory in plain language"))
        self.assertLess(skill.index("Explain the memory in plain language"), skill.index("Choose the starting point"))
        self.assertLess(skill.index("Mandatory dependency gate"), skill.index("Choose the starting point"))
        self.assertLess(skill.index("Choose the starting point"), skill.index("Interview progressively"))
        self.assertLess(skill.index("Mandatory dependency gate"), skill.index("Interview progressively"))

    def test_social_skill_requires_explanation_auth_test_and_schedule(self) -> None:
        skill = (ROOT / "skills/wiki-memory-social-sync/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("First activation contract", skill)
        self.assertIn("daily, weekly, or another cadence", skill)
        self.assertIn("sign in interactively", skill)
        self.assertIn("one interactive test sync", skill)
        self.assertIn("Sources/items/<platform>/<collection>/", skill)
        self.assertIn("Never ask the user to paste a password", skill)

    def test_manifest_and_schemas_are_valid_json(self) -> None:
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "wiki-memory")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["version"], "1.0.0-alpha.15")
        self.assertEqual(manifest["interface"]["defaultPrompt"][0], "Commençons.")
        self.assertTrue(all("$wiki-memory" not in prompt for prompt in manifest["interface"]["defaultPrompt"]))
        self.assertTrue(all("Use " not in prompt for prompt in manifest["interface"]["defaultPrompt"]))
        self.assertTrue((ROOT / manifest["interface"]["composerIcon"]).is_file())
        self.assertTrue((ROOT / manifest["interface"]["logo"]).is_file())
        for screenshot in manifest["interface"]["screenshots"]:
            self.assertTrue((ROOT / screenshot).is_file())
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(marketplace["name"], "petitmaker")
        self.assertEqual(marketplace["plugins"][0]["source"]["source"], "url")
        self.assertEqual(marketplace["plugins"][0]["name"], manifest["name"])
        for metadata in (ROOT / "skills").glob("*/agents/openai.yaml"):
            copy = metadata.read_text(encoding="utf-8")
            self.assertNotIn("Use $wiki-memory", copy)
        agent_instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("continue in the same conversation in French", agent_instructions)
        self.assertIn("Veux-tu démarrer un échange", agent_instructions)
        for schema in (ROOT / "schemas").glob("*.json"):
            self.assertIsInstance(json.loads(schema.read_text(encoding="utf-8")), dict)
        temporal = json.loads((ROOT / "schemas" / "temporal-frontmatter.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("required", temporal)
        self.assertTrue(
            {"valid_from", "valid_until", "recorded_at", "invalidated_at", "supersedes", "superseded_by"}
            <= set(temporal["properties"])
        )

    def test_release_versions_are_aligned_across_runtimes_and_deployment(self) -> None:
        """One release identity must drive the plugin, wheel, chart, and docs."""

        plugin = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        release_version = plugin["version"]
        match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?", release_version)
        self.assertIsNotNone(match, "releases must use MAJOR.MINOR.PATCH[-alpha.N|-beta.N|-rc.N]")
        assert match is not None
        prerelease_kind, prerelease_number = match.group(2), match.group(3)
        pep440_suffixes = {"alpha": "a", "beta": "b", "rc": "rc"}
        package_version = (
            match.group(1)
            if prerelease_kind is None
            else f"{match.group(1)}{pep440_suffixes[prerelease_kind]}{prerelease_number}"
        )

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{package_version}"', pyproject)
        init_module = (ROOT / "src" / "wiki_memory" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn(f'__version__ = "{package_version}"', init_module)
        self.assertEqual(load_data(ROOT / "deploy" / "helm" / "wiki-memory" / "Chart.yaml")["version"], release_version)
        self.assertEqual(
            load_data(ROOT / "deploy" / "helm" / "wiki-memory" / "Chart.yaml")["appVersion"], release_version
        )
        self.assertEqual(
            load_data(ROOT / "deploy" / "helm" / "wiki-memory" / "values.yaml")["image"]["tag"], release_version
        )
        self.assertIn(f"Current version: `{release_version}`", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn(f"Version actuelle : `{release_version}`", (ROOT / "README.fr.md").read_text(encoding="utf-8"))
        self.assertIn(f"## [{release_version}]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
