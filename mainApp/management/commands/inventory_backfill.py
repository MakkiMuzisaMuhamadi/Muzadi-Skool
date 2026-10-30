# mainApp/management/commands/inventory_backfill.py
"""
Run once after migrating:   python manage.py inventory_backfill

1. Copies the due date of each current loan onto the unit (used for the fixed overdue check).
2. Closes old ISSUE rows that were returned before `returned_at` was tracked on them.
3. Links staff borrowers typed as names to their user accounts where the name matches exactly.

Safe to run more than once.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from mainApp.models.inventory_models import InventoryTransaction, InventoryUnit

CLOSING_TYPES = ("RETURN", "DEALLOCATE", "LOST", "DISPOSE")


class Command(BaseCommand):
    help = "Backfill inventory loan fields after the inventory upgrade."

    @transaction.atomic
    def handle(self, *args, **options):
        closed = due_set = linked = 0

        for unit in InventoryUnit.objects.all().iterator():
            txs = list(InventoryTransaction.objects.filter(unit=unit).order_by("created_at", "id")
                       .only("id", "tx_type", "created_at", "returned_at", "due_date"))
            open_issue = None
            for i, tx in enumerate(txs):
                if tx.tx_type not in ("ISSUE", "ALLOCATE") or tx.returned_at:
                    continue
                closer = next((t for t in txs[i + 1:] if t.tx_type in CLOSING_TYPES + ("ISSUE", "ALLOCATE")), None)
                if closer:
                    InventoryTransaction.objects.filter(pk=tx.pk).update(returned_at=closer.created_at)
                    closed += 1
                elif unit.status in ("ISSUED", "ALLOCATED"):
                    open_issue = tx
                else:
                    InventoryTransaction.objects.filter(pk=tx.pk).update(returned_at=timezone.now())
                    closed += 1

            updates = {}
            if unit.status == "ISSUED" and open_issue and not unit.current_due_date:
                updates["current_due_date"] = open_issue.due_date
                due_set += 1
            if unit.status == "ISSUED" and not unit.current_loan_kind:
                updates["current_loan_kind"] = "LOAN"
            if unit.status == "ALLOCATED" and not unit.current_loan_kind:
                updates["current_loan_kind"] = "ALLOCATION"
            if unit.current_staff_name and not unit.current_staff_id and not unit.current_student_id:
                first, _, last = unit.current_staff_name.strip().partition(" ")
                matches = User.objects.filter(profile__school_id=unit.school_id, first_name__iexact=first,
                                              last_name__iexact=last.strip())
                if matches.count() == 1:
                    updates["current_staff"] = matches.first()
                    linked += 1
            if updates:
                InventoryUnit.objects.filter(pk=unit.pk).update(**updates)

        self.stdout.write(self.style.SUCCESS(
            f"Closed {closed} old loan rows, set {due_set} due dates, linked {linked} staff holders."))
