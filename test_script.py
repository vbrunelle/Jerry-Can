import time
from prices.models import Analysis
from prices.views import _analysis_storage_estimate

for pk in [1, 10]:
    try:
        analysis = Analysis.objects.filter(pk=pk).first()
        if not analysis:
            print(f"Analysis #{pk}: Not found")
            continue
        t = time.time()
        est = _analysis_storage_estimate(analysis)
        elapsed = time.time() - t
        print(f"Analysis #{pk}: _analysis_storage_estimate() -> {elapsed:.3f}s, available={est['available']}, reason={est.get('reason','')}")
    except Exception as e:
        import traceback
        print(f"Analysis #{pk}: ERROR {type(e).__name__}: {e}")
        traceback.print_exc()
