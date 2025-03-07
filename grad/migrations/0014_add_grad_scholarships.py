# Generated by Django 3.2.14 on 2025-01-28 11:16

import courselib.conditional_save
import courselib.json_fields
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('grad', '0013_add_grad_categories'),
    ]

    operations = [
        migrations.CreateModel(
            name='GradScholarship',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(max_length=100)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=8)),
                ('eligible', models.BooleanField(default=True)),
                ('comments', models.TextField(blank=True, null=True)),
                ('removed', models.BooleanField(default=False)),
                ('config', courselib.json_fields.JSONField(default=dict)),
                ('semester', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='coredata.semester')),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='grad.gradstudent')),
            ],
            bases=(models.Model, courselib.conditional_save.ConditionalSaveMixin),
        ),
    ]
