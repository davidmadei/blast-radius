import os
OUT = os.environ.get("FAILREC_OUT")
def pytest_runtest_logreport(report):
    if OUT and report.failed:
        with open(OUT, "a") as f:
            f.write(report.nodeid + "\n")
def pytest_collectreport(report):
    if OUT and report.failed:
        with open(OUT, "a") as f:
            f.write("COLLECT " + report.nodeid + "\n")
