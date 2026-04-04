import django, os


def main():
    if os.environ.get('CONFIRM_REFRESH_INSPECTION_CACHE') != '1':
        print(
            "Refusing to modify InspectionCache without explicit confirmation. "
            "Set CONFIRM_REFRESH_INSPECTION_CACHE=1 to run this script."
        )
        return

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


if __name__ == "__main__":
    main()
