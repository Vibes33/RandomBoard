"""Chaos absolu (4.2) : PoolConfig, Infection (Peste & Choléra), StackLedger."""
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_seed_new_rules"),
    ]

    operations = [
        migrations.CreateModel(
            name="PoolConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("rank_mult_active", models.BooleanField(default=False)),
                ("rank_mult_first", models.DecimalField(decimal_places=3, default=Decimal("1.001"),
                                                        max_digits=6)),
                ("rank_mult_last", models.DecimalField(decimal_places=3, default=Decimal("1.141"),
                                                       max_digits=6)),
                ("stacking_active", models.BooleanField(default=False)),
                ("stacking_penalty_pct", models.DecimalField(decimal_places=2, default=Decimal("5"),
                                                             max_digits=6)),
                ("stacking_endgame_buff", models.DecimalField(decimal_places=2, default=Decimal("500"),
                                                              max_digits=10)),
                ("plague_seeded", models.BooleanField(default=False)),
                ("plague_payout", models.DecimalField(decimal_places=2, default=Decimal("50"),
                                                      max_digits=10)),
                ("pool", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                              related_name="config", to="core.pool")),
            ],
        ),
        migrations.CreateModel(
            name="StackLedger",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("total_stacked", models.DecimalField(decimal_places=2, default=Decimal("0"),
                                                      max_digits=12)),
                ("pool", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="stacks", to="core.pool")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="stacks", to="core.appuser")),
            ],
            options={"unique_together": {("pool", "user")}},
        ),
        migrations.CreateModel(
            name="Infection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("disease", models.CharField(choices=[("peste", "Peste"), ("cholera", "Choléra")],
                                             max_length=8)),
                ("is_patient_zero", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("pool", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="infections", to="core.pool")),
                ("source", models.ForeignKey(blank=True, null=True,
                                             on_delete=django.db.models.deletion.SET_NULL,
                                             related_name="contaminations", to="core.appuser")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="infections", to="core.appuser")),
            ],
            options={
                "verbose_name": "Infection",
                "verbose_name_plural": "Infections",
                "ordering": ("-created_at",),
                "unique_together": {("pool", "user")},
            },
        ),
    ]
