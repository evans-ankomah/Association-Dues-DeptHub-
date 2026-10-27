"""Turn the placeholder supporting_document_id UUID columns into real foreign keys to documents.

The column name and type stay the same, so existing values are kept: the model state becomes a
ForeignKey and the database only gains the foreign-key constraint and index.
"""

import django.db.models.deletion
from django.db import migrations, models

FK = dict(
    blank=True,
    null=True,
    on_delete=django.db.models.deletion.PROTECT,
    related_name="+",
    to="documents.document",
)


def _swap(model_name):
    return migrations.SeparateDatabaseAndState(
        state_operations=[
            migrations.RemoveField(model_name=model_name, name="supporting_document_id"),
            migrations.AddField(model_name=model_name, name="supporting_document", field=models.ForeignKey(**FK)),
        ],
        database_operations=[
            migrations.AlterField(
                model_name=model_name,
                name="supporting_document_id",
                field=models.ForeignKey(db_column="supporting_document_id", **FK),
            ),
        ],
    )


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0001_initial"),
        ("finance", "0005_budgets_and_expenses"),
    ]

    operations = [_swap("expense"), _swap("expenserequest")]
