from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('prices', '0009_chunkedupload'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistransfertask',
            name='activity_log',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='analysistransfertask',
            name='progress_percent',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='analysistransfertask',
            name='status_detail',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
