from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('prices', '0010_analysistransfertask_status_logging'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysistransfertask',
            name='cancel_requested',
            field=models.BooleanField(default=False),
        ),
    ]
