"""
Parents & Guardians upgrade.

What this migration does (data is preserved):
  1. Adds Ugandan address fields, language and communication consent to Parent.
  2. Copies Parent.state into Parent.district, then drops state / postal_code.
  3. Normalises every stored phone number to +256... format.
  4. Creates ParentSequence (per-school Parent ID counter) and seeds it from existing IDs.
  5. Creates StudentGuardian and copies every existing student<->parent link into it,
     carrying over relationship / primary / emergency flags from the parent.
  6. Switches Student.parents to use StudentGuardian as its through table.

Back up the database first. Step 6 is not reversible.
"""
import re

import django.db.models.deletion
from django.db import migrations, models

RELATIONSHIP_CHOICES = [
    ("FATHER", "Father"), ("MOTHER", "Mother"), ("GUARDIAN", "Guardian"),
    ("GRANDFATHER", "Grandfather"), ("GRANDMOTHER", "Grandmother"),
    ("UNCLE", "Uncle"), ("AUNT", "Aunt"), ("SIBLING", "Brother/Sister"),
    ("SPONSOR", "Sponsor"), ("OTHER", "Other"),
]


def _normalize(raw):
    # Frozen copy of mainApp.parent_utils.normalize_phone (migrations must not change later).
    if not raw:
        return ""
    s = re.sub(r"[\s\-().]", "", str(raw).strip())
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        return s
    if s.startswith("256") and len(s) == 12:
        return "+" + s
    if s.startswith("0") and len(s) == 10:
        return "+256" + s[1:]
    if s.startswith("7") and len(s) == 9:
        return "+256" + s
    return s


def forwards_parent_data(apps, schema_editor):
    Parent = apps.get_model("mainApp", "Parent")
    ParentSequence = apps.get_model("mainApp", "ParentSequence")
    db = schema_editor.connection.alias

    max_by_school = {}
    for p in Parent.objects.using(db).all().iterator():
        changed = []
        full = " ".join(x for x in [p.first_name, p.middle_name, p.last_name] if x).strip()
        if full != p.full_name:
            p.full_name = full
            changed.append("full_name")
        if p.state and not p.district:
            p.district = p.state
            changed.append("district")
        for field in ("phone", "alternate_phone", "whatsapp_number"):
            old = getattr(p, field)
            new = _normalize(old) if old else old
            if new != old:
                setattr(p, field, new)
                changed.append(field)
        if p.email and p.email != p.email.strip().lower():
            p.email = p.email.strip().lower()
            changed.append("email")
        if changed:
            p.save(update_fields=changed)

        prefix = f"PAR{p.school_id}"
        pid = p.parent_id or ""
        tail = pid[len(prefix):]
        if pid.startswith(prefix) and len(tail) == 5 and tail.isdigit():
            max_by_school[p.school_id] = max(max_by_school.get(p.school_id, 0), int(tail))

    for school_id, last in max_by_school.items():
        ParentSequence.objects.using(db).update_or_create(
            school_id=school_id, defaults={"last_number": last}
        )


def forwards_copy_links(apps, schema_editor):
    Student = apps.get_model("mainApp", "Student")
    StudentGuardian = apps.get_model("mainApp", "StudentGuardian")
    db = schema_editor.connection.alias

    OldThrough = Student._meta.get_field("parents").remote_field.through
    batch = []
    for link in OldThrough.objects.using(db).select_related("parent").iterator():
        p = link.parent
        batch.append(StudentGuardian(
            student_id=link.student_id,
            parent_id=link.parent_id,
            relationship=p.relationship or "GUARDIAN",
            is_primary=bool(p.is_primary_contact),
            is_emergency_contact=bool(p.is_emergency_contact),
            is_fee_payer=bool(p.is_primary_contact),
        ))
        if len(batch) >= 1000:
            StudentGuardian.objects.using(db).bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        StudentGuardian.objects.using(db).bulk_create(batch, ignore_conflicts=True)

    # Exactly one primary contact per student.
    student_ids = StudentGuardian.objects.using(db).values_list("student_id", flat=True).distinct()
    for sid in student_ids.iterator():
        links = list(StudentGuardian.objects.using(db).filter(student_id=sid).order_by("-is_primary", "id"))
        keep = links[0]
        if not keep.is_primary:
            keep.is_primary = True
            keep.save(update_fields=["is_primary"])
        extra = [l.id for l in links[1:] if l.is_primary]
        if extra:
            StudentGuardian.objects.using(db).filter(id__in=extra).update(is_primary=False)


def flush_deferred_constraints(apps, schema_editor):
    # PostgreSQL: the data steps above leave deferred FK checks pending, and Postgres
    # refuses ALTER TABLE on those tables until they run. Check them now.
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")


class Migration(migrations.Migration):

    dependencies = [
        ("mainApp", "0087_feereceiptsequence_remove_receiptsequence_school_and_more"),
    ]

    operations = [
        # ---- 1. New Parent fields --------------------------------------------
        migrations.AddField(
            model_name="parent", name="district",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="parent", name="sub_county",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="parent", name="village",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="parent", name="preferred_language",
            field=models.CharField(
                choices=[("EN", "English"), ("LG", "Luganda"), ("SW", "Swahili"), ("OTHER", "Other")],
                default="EN", max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="parent", name="sms_consent",
            field=models.BooleanField(default=True, verbose_name="Agrees to receive SMS"),
        ),
        migrations.AddField(
            model_name="parent", name="whatsapp_consent",
            field=models.BooleanField(default=True, verbose_name="Agrees to receive WhatsApp messages"),
        ),
        migrations.AddField(
            model_name="parent", name="consent_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="parent", name="parent_id",
            field=models.CharField(editable=False, max_length=50, unique=True),
        ),
        migrations.AlterField(
            model_name="parent", name="relationship",
            field=models.CharField(choices=RELATIONSHIP_CHOICES, default="GUARDIAN", max_length=20),
        ),
        migrations.AlterField(
            model_name="parent", name="occupation",
            field=models.CharField(
                blank=True, null=True, max_length=20,
                choices=[("BUSINESS", "Business"), ("SERVICE", "Service"), ("PROFESSIONAL", "Professional"),
                         ("FARMER", "Farmer"), ("RETIRED", "Retired"), ("HOUSEWIFE", "Homemaker"),
                         ("OTHER", "Other")],
            ),
        ),
        migrations.AlterField(
            model_name="parent", name="phone",
            field=models.CharField(db_index=True, max_length=20),
        ),
        migrations.AlterField(
            model_name="parent", name="city",
            field=models.CharField(blank=True, max_length=50, null=True, verbose_name="Town / City"),
        ),
        migrations.AlterField(
            model_name="parent", name="address",
            field=models.TextField(blank=True, default="", verbose_name="Other address details"),
        ),

        # ---- 2. Parent ID sequence ------------------------------------------
        migrations.CreateModel(
            name="ParentSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("last_number", models.PositiveIntegerField(default=0)),
                ("school", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="parent_sequence", to="mainApp.school")),
            ],
        ),

        # ---- 3. Per-child guardian link -------------------------------------
        migrations.CreateModel(
            name="StudentGuardian",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("relationship", models.CharField(choices=RELATIONSHIP_CHOICES, default="GUARDIAN", max_length=20)),
                ("is_primary", models.BooleanField(default=False, verbose_name="Primary contact")),
                ("is_emergency_contact", models.BooleanField(default=False, verbose_name="Emergency contact")),
                ("is_fee_payer", models.BooleanField(default=False, verbose_name="Pays fees")),
                ("can_pick_up", models.BooleanField(default=True, verbose_name="Allowed to pick up")),
                ("receives_sms", models.BooleanField(default=True, verbose_name="Receives SMS about this child")),
                ("created_at", models.DateTimeField(auto_now_add=True, blank=True, null=True)),
                ("parent", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="child_links", to="mainApp.parent")),
                ("student", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="guardian_links", to="mainApp.student")),
            ],
            options={
                "ordering": ["-is_primary", "id"],
                "unique_together": {("student", "parent")},
            },
        ),

        # ---- 4. Data --------------------------------------------------------
        migrations.RunPython(forwards_parent_data, migrations.RunPython.noop),
        migrations.RunPython(forwards_copy_links, migrations.RunPython.noop),
        migrations.RunPython(flush_deferred_constraints, migrations.RunPython.noop),

        # ---- 5. Drop fields that moved or no longer apply -------------------
        migrations.RemoveField(model_name="parent", name="state"),
        migrations.RemoveField(model_name="parent", name="postal_code"),
        migrations.RemoveField(model_name="parent", name="is_primary_contact"),
        migrations.RemoveField(model_name="parent", name="is_emergency_contact"),
        migrations.AddIndex(
            model_name="parent",
            index=models.Index(fields=["school", "phone"], name="mainApp_par_school__phone_idx"),
        ),
        migrations.AddIndex(
            model_name="parent",
            index=models.Index(fields=["school", "is_active"], name="mainApp_par_school__active_idx"),
        ),

        # ---- 6. Point Student.parents at the new through table --------------
        migrations.RemoveField(model_name="student", name="parents"),
        migrations.AddField(
            model_name="student", name="parents",
            field=models.ManyToManyField(
                blank=True, related_name="children",
                through="mainApp.StudentGuardian", to="mainApp.parent"),
        ),
    ]
