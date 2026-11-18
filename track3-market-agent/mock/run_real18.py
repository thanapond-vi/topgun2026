"""Run mock/run_games.py with the 18 Nov practice market's numbers (temporary, does not edit the mock)."""
import runpy, sys
sys.path.insert(0, "mock"); sys.path.insert(0, ".")
import mock_market
mock_market.SPECS["SPECIALTY"] = [{"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 16},
                                  {"max_defect_pct": 3.0, "max_moisture_pct": 12.5, "min_screen": 17}]
# COMMERCIAL ~0.98-1.05 (1.06 came back ABOVE_LIMIT), INSTANT ~0.90-0.97, SPECIALTY unknown (nobody sold)
mock_market.CEILINGS.update({"COMMERCIAL": (0.98, 1.06), "INSTANT": (0.90, 0.97), "SPECIALTY": (1.10, 1.30)})
sys.argv = ["run_games.py"] + sys.argv[1:]
runpy.run_path("mock/run_games.py", run_name="__main__")
