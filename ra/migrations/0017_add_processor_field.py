# Generated by Django 2.2.15 on 2021-10-07 12:02

import django.core.files.storage
from django.db import migrations, models
import django.db.models.deletion
import ra.models


class Migration(migrations.Migration):

    dependencies = [
        ('coredata', '0025_update_choices'),
        ('ra', '0016_add_drafts'),
    ]

    operations = [
        migrations.AddField(
            model_name='rarequest',
            name='processor',
            field=models.ForeignKey(default=None, editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='rarequest_processor', to='coredata.Person'),
        ),
    ]