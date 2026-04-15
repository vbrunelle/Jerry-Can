from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('prices', '0005_snapshot_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='analysis',
            name='run_automatically',
            field=models.BooleanField(
                default=False,
                help_text='Automatically take snapshots at the configured frequency.',
            ),
        ),
    ]
