"""Repository checks — what a pull request from a stranger relies on.

    ./venv/bin/python -m unittest tests.test_repo -v

The collectors' own tests are in test_collectors.py. This file guards the
scaffolding around them: that the CI workflow cannot be given write access
or secrets by a later edit, and that the contributor guide, the pull request
template and the issue forms exist and say what they must.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GITHUB = ROOT / ".github"


def workflows() -> list[Path]:
    return sorted((GITHUB / "workflows").glob("*.y*ml"))


class TestWorkflows(unittest.TestCase):
    """CI runs code from forks. It must never run that code with more than a
    read-only token."""

    def test_the_suite_runs_on_pull_requests(self):
        """One workflow runs the whole test directory on `pull_request`."""
        self.assertTrue(workflows(), "no workflow under .github/workflows")
        runs_suite = []
        for path in workflows():
            wf = yaml.safe_load(path.read_text())
            triggers = wf.get("on", wf.get(True))      # YAML reads a bare `on` as True
            if "pull_request" in triggers and "unittest discover -s tests" in path.read_text():
                runs_suite.append(path.name)
        self.assertTrue(runs_suite, "no workflow runs `unittest discover -s tests` on pull_request")

    def test_no_workflow_can_write_or_read_secrets(self):
        """Every workflow: no `pull_request_target` or `workflow_run` trigger
        (both run with the base repository's rights on a fork's code), a
        top-level `permissions` block that grants nothing beyond reading the
        contents, no job that widens it, and no secret."""
        for path in workflows():
            text = path.read_text()
            wf = yaml.safe_load(text)
            triggers = wf.get("on", wf.get(True))
            names = {triggers} if isinstance(triggers, str) else set(triggers)
            self.assertFalse(names & {"pull_request_target", "workflow_run"},
                             f"{path.name}: a trigger that runs fork code with this repository's rights")
            self.assertEqual(wf.get("permissions"), {"contents": "read"},
                             f"{path.name}: top-level permissions must be exactly `contents: read`")
            for job, spec in wf["jobs"].items():
                self.assertNotIn("permissions", spec, f"{path.name}: job {job} sets its own permissions")
            self.assertNotRegex(text, r"\$\{\{\s*secrets\.", f"{path.name}: uses a secret")

    def test_actions_are_pinned_to_a_commit(self):
        """A `uses:` names a full commit, not a tag that can be moved."""
        for path in workflows():
            for ref in re.findall(r"^\s*-?\s*uses:\s*(\S+)", path.read_text(), re.M):
                self.assertRegex(ref, r"@[0-9a-f]{40}$", f"{path.name}: {ref} is not pinned to a commit")


class TestContributorDocs(unittest.TestCase):
    """The guide, the template and the forms exist and carry the rules."""

    def test_contributing_states_the_sourcing_rules_and_the_test_command(self):
        text = (ROOT / "CONTRIBUTING.md").read_text()
        for needle in ("Government or court origin", "phone numbers", "Link every record",
                       "No collected data in this repository", "Doe", "gov_layers/",
                       "unittest discover -s tests"):
            self.assertIn(needle, text)
        self.assertIn("CONTRIBUTING.md", (ROOT / "README.md").read_text())

    def test_contributing_names_files_that_exist(self):
        """Every repository path the guide puts in backticks is there."""
        text = (ROOT / "CONTRIBUTING.md").read_text()
        ignored = {"records/", "liens/", "courts/", "news/", ".cache/"}     # git-ignored outputs
        for ref in sorted(set(re.findall(r"`([\w./<>-]+/[\w./<>-]*|[\w.-]+\.(?:md|yml|txt|json))`", text))):
            if ref in ignored:
                continue
            target = ref.split("<")[0]                  # gov_layers/<ST>.yml -> gov_layers/
            hits = [target] if (ROOT / target).exists() else list(ROOT.rglob(target))
            self.assertTrue(hits, f"CONTRIBUTING.md names `{ref}`, which is not in the repository")

    def test_pull_request_template_asks_for_the_rules_evidence_and_tests(self):
        text = (GITHUB / "PULL_REQUEST_TEMPLATE.md").read_text()
        for needle in ("Government or court origin", "No collected data", "Evidence it works",
                       "tests/fixtures/", "Doe"):
            self.assertIn(needle, text)

    def test_issue_forms_are_valid(self):
        """Each form has what GitHub requires of one (name, description, a
        body whose inputs carry unique ids), and blank issues plus the
        private hand-off link for collected data are configured."""
        forms = sorted(p for p in (GITHUB / "ISSUE_TEMPLATE").glob("*.yml") if p.name != "config.yml")
        self.assertEqual([p.stem for p in forms], ["new_source", "source_blocked"])
        for path in forms:
            form = yaml.safe_load(path.read_text())
            self.assertTrue(form["name"] and form["description"], path.name)
            ids = [f["id"] for f in form["body"] if f["type"] != "markdown"]
            self.assertTrue(ids, f"{path.name}: no input")
            self.assertEqual(len(ids), len(set(ids)), f"{path.name}: duplicate ids")
            for field in form["body"]:
                self.assertIn(field["type"], {"markdown", "input", "textarea", "dropdown", "checkboxes"})
                if field["type"] != "markdown":
                    self.assertTrue(field["attributes"]["label"], f"{path.name}: {field['id']} has no label")
        config = yaml.safe_load((GITHUB / "ISSUE_TEMPLATE" / "config.yml").read_text())
        self.assertTrue(any("collected" in link["name"].lower() for link in config["contact_links"]))


if __name__ == "__main__":
    unittest.main()
