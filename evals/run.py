from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db import Database, canonical_json
from app.models import utc_now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/professional-evals.db")
    args = parser.parse_args()
    database = Database(ROOT / args.db); database.initialize()
    run_id, now = "proof-" + uuid.uuid4().hex[:10], utc_now()
    with tempfile.TemporaryDirectory() as temp:
        report = Path(temp)/"results.xml"
        result = subprocess.run([sys.executable,"-m","pytest","-q","tests/test_closed_loop.py","tests/test_evidence_spans.py","tests/test_condition_registry.py","tests/test_policy.py",f"--junitxml={report}"], cwd=ROOT, capture_output=True, text=True)
        if not report.exists():
            raise SystemExit("Evaluation runner failed before producing test results")
        root = ET.parse(report).getroot()
        results = []
        with database.transaction() as conn:
            for test in root.iter("testcase"):
                passed = not any(test.find(tag) is not None for tag in ("failure","error","skipped"))
                scenario = test.attrib.get("classname", "") + "." + test.attrib["name"]
                mode = "fixture-unit"
                actual = {"summary": "Executed pytest test: " + test.attrib["name"], "duration_seconds": float(test.attrib.get("time", 0))}
                conn.execute("INSERT INTO eval_results (id,suite_run_id,scenario_id,mode,model_id,prompt_version,expected_json,actual_json,metrics_json,passed,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()),run_id,scenario,mode,"fixture-deterministic-v1","professional-v1",canonical_json({"pass":True}),canonical_json(actual),canonical_json({"duration_seconds":actual["duration_seconds"]}),int(passed),now))
                results.append(passed)
    print(json.dumps({"run_id":run_id,"passed":sum(results),"total":len(results),"proof":"executed fixture/unit tests; no live provider claims"},indent=2))
    if result.returncode or not all(results): raise SystemExit(1)


if __name__ == "__main__": main()
