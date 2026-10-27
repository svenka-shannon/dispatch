"""2026-10-01: FedEx single-package page legend made the digest say "Label created, no ETA" on delivery day."""
import importlib.machinery, importlib.util, pathlib
src = pathlib.Path.home()/"dispatch/bin/track"
ld = importlib.machinery.SourceFileLoader("trk", str(src)); sp = importlib.util.spec_from_loader("trk", ld); m = importlib.util.module_from_spec(sp); ld.exec_module(m)
legend = "\nLabel created\nOn the way\nOut for delivery\nDelivered\n"
cases = {
 "delivered": "#\n383907014412\nThursday\n10/01/2026 at 10:10 am\nSigned for by: Signature not required\nView more details\nFROM\n",
 "today": "#\n383907014412\nToday\nBy end of day\nCurrently in South San Francisco, CA\nFROM\nSan Marcos, TX US" + legend,
 "thursday": "#\n383907014412\nThursday\nby end of day\nCurrently in Cypress, TX\nSan Marcos, TX US\nPacifica, CA US" + legend,
 "delayed": "#\n383907014412\nWe're sorry. Your package wasn't sent out when expected. We're working on this and will get you a new delivery date.\nCurrently in Cypress, TX\nSan Marcos, TX US" + legend,
}
import pytest
@pytest.mark.parametrize("k", list(cases))
def test_case(k):
    r = m.parse_fedex_single(cases[k], "383907014412")
    exp = {"delivered": "Delivered", "today": "Arriving today", "thursday": "On the way", "delayed": "Delayed"}[k]
    assert r["status"] == exp
    if k != "delayed" and k != "delivered": assert r["eta"]
if False:
  for k, t in cases.items():
    print(k, m.parse_fedex_single(t, "383907014412"))
