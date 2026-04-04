from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='inspectioncache',
            name='duration_seconds',
            field=models.FloatField(blank=True, null=True),
        ),
    ]
