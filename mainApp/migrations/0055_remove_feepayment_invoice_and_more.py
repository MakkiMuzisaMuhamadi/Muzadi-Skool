from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("mainApp", "0054_term_is_final_term_term_is_marks_entry_open_and_more"),
    ]

    operations = [
        migrations.DeleteModel(
            name="FeeInvoiceLine",
        ),
        migrations.DeleteModel(
            name="FeeInvoice",
        ),
        migrations.DeleteModel(
            name="FeePayment",
        ),
        migrations.DeleteModel(
            name="FeeStructureLine",
        ),
        migrations.DeleteModel(
            name="FeeStructure",
        ),
        migrations.DeleteModel(
            name="FeeItem",
        ),
    ]