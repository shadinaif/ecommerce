# Generated by Django 2.2.15 on 2020-12-18 21:38

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payment', '0026_auto_20200305_1448'),
    ]

    operations = [
        migrations.CreateModel(
            name='IyzicoProcessorConfiguration',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('retry_attempts', models.PositiveSmallIntegerField(default=0, verbose_name='Number of times to retry failing Iyzico client actions (e.g., payment creation, payment execution)')),
            ],
            options={
                'verbose_name': 'Iyzico Processor Configuration',
            },
        ),
        migrations.CreateModel(
            name='IyzicoWebProfile',
            fields=[
                ('id', models.CharField(max_length=255, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=255, unique=True)),
            ],
        ),
    ]