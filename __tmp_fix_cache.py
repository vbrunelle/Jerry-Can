import django, os
os.environ['DJANGO_SETTINGS_MODULE'] = 'jerrycan_web.settings'
django.setup()
from dashboard.models import InspectionCache
from dashboard.services import refresh_inspection_cache
InspectionCache.objects.all().delete()
print('Cache deleted')
refresh_inspection_cache()
c = InspectionCache.objects.first()
if c:
    snaps = c.data.get('snapshots', [])
    print('Snapshots:')
    for s in snaps[:5]:
        print(s)
else:
    print('no cache built')
