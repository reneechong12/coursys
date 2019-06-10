# Generated by Django 2.0.13 on 2019-04-29 13:54

import autoslug.fields
import courselib.json_fields
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('space', '0007_no_null_campus_and_building'),
    ]

    operations = [
        migrations.CreateModel(
            name='RoomSafetyItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=50)),
                ('description', models.CharField(blank=True, max_length=500, null=True)),
                ('hidden', models.BooleanField(default=False, editable=False)),
                ('config', courselib.json_fields.JSONField(default=dict, editable=False)),
                ('slug', autoslug.fields.AutoSlugField(editable=False, populate_from='autoslug', unique=True)),
                ('unit', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='coredata.Unit')),
            ],
        ),
        migrations.AddField(
            model_name='location',
            name='field',
            field=models.CharField(blank=True, max_length=250, null=True, verbose_name='Research/Teaching Field'),
        ),
        migrations.AddField(
            model_name='location',
            name='safety_items',
            field=models.ManyToManyField(blank=True, to='space.RoomSafetyItem', verbose_name='Safety Infrastructure'),
        ),
    ]