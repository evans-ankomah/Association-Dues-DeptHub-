"""Turn the placeholder minutes_document_id UUID column into a real foreign key to documents,
keeping existing values (same column name and type)."""

import django.db.models.deletion
from django.db import migrations, models

FK = dict(
    blank=True,
    null=True,
    on_delete=django.db.models.deletion.PROTECT,
    related_name="minutes_for",
    to="documents.document",
)


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0001_initial"),
        ("workspace", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="meeting", name="minutes_document_id"),
                migrations.AddField(model_name="meeting", name="minutes_document", field=models.ForeignKey(**FK)),
            ],
            database_operations=[
                migrations.AlterField(
                    model_name="meeting",
                    name="minutes_document_id",
                    field=models.ForeignKey(db_column="minutes_document_id", **FK),
                ),
            ],
        )
    ]
