"""The committed n8n JSON must match the generator and be structurally sound."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT

N8N_DIR = ROOT / "n8n"
STUB_GLOBALS = (
    "const $input={all(){return[]},first(){return{json:{data:''}}}};"
    "const $=()=>({all(){return[]}});"
    "const $today={minus(){return this},toFormat(){return ''}};"
    "const $execution={id:'x'};const $json={tech_stack:[]};\n"
)


class WorkflowTests(unittest.TestCase):
    def load(self, name: str) -> dict:
        return json.loads((N8N_DIR / name).read_text(encoding="utf-8"))

    def test_generator_output_is_committed(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_workflows", N8N_DIR / "build_workflows.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, builder in (("domain_flipper.workflow.json", module.build_domain_flipper), ("saas_scout.workflow.json", module.build_saas_scout)):
            self.assertEqual(self.load(name), builder(), f"{name} is stale: run python3 n8n/build_workflows.py")

    def test_structure(self):
        for name in ("domain_flipper.workflow.json", "saas_scout.workflow.json"):
            wf = self.load(name)
            names = [n["name"] for n in wf["nodes"]]
            self.assertEqual(len(names), len(set(names)), "node names must be unique")
            self.assertEqual(sum(n["type"] == "n8n-nodes-base.scheduleTrigger" for n in wf["nodes"]), 1)
            for src, conn in wf["connections"].items():
                self.assertIn(src, names)
                for target in conn["main"][0]:
                    self.assertIn(target["node"], names)
            for node in wf["nodes"]:
                self.assertIn("typeVersion", node)
                if node["type"] == "n8n-nodes-base.httpRequest":
                    self.assertTrue(node.get("retryOnFail"))
            self.assertTrue(any("Slack" in n["name"] for n in wf["nodes"]))

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_embedded_javascript_parses(self):
        for name in ("domain_flipper.workflow.json", "saas_scout.workflow.json"):
            wf = self.load(name)
            for node in wf["nodes"]:
                snippets = []
                if node["type"] == "n8n-nodes-base.code":
                    snippets.append("(async()=>{" + node["parameters"]["jsCode"] + "})")
                body = node.get("parameters", {}).get("jsonBody", "")
                if body.startswith("={{ (() =>"):
                    snippets.append(body[len("={{ "):-len(" }}")] + ";")
                for js in snippets:
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp) / "snippet.js"
                        path.write_text(STUB_GLOBALS + js, encoding="utf-8")
                        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, f"{name} / {node['name']}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
