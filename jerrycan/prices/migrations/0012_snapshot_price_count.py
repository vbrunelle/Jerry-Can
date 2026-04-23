from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('prices', '0011_analysistransfertask_cancel_requested'),
    ]

    operations = [
        migrations.AddField(
            model_name='snapshot',
            name='price_count',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
