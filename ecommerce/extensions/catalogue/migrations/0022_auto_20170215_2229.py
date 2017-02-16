# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import oscar.models.fields.slugfield


class Migration(migrations.Migration):

    dependencies = [
        ('catalogue', '0021_auto_20170215_2224'),
    ]

    operations = [
        migrations.AlterField(
            model_name='category',
            name='slug',
            field=oscar.models.fields.slugfield.SlugField(max_length=255, verbose_name='Slug'),
        ),
    ]
