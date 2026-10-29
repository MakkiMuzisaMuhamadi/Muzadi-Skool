"""
Student import engine (school onboarding + routine bulk upload).

Flow
----
1. ``read_sheet_structure``   find the header row and headers in the uploaded workbook
2. ``auto_map_columns``       guess which file column feeds which system field
3. ``stage_batch``            read every row into StudentBulkUploadRow, validate, flag duplicates
4. (admin reviews, edits rows, picks CREATE / SKIP / REVIEW)
5. ``import_batch``           create students, parents, opening balances; optionally queue accounts

Steps 3 and 5 run in Celery (see mainApp/tasks/student_import_tasks.py) and report progress
on the batch, so a school with thousands of learners never hits a web timeout.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from mainApp.parent_utils import link_guardian
from mainApp.models.models import Class
from mainApp.models.student import (
    Parent,
    Student,
    StudentBulkUploadRow,
    normalize_student_name,
)

logger = logging.getLogger(__name__)

PROGRESS_EVERY = 25
STAGE_CHUNK = 500


# ---------------------------------------------------------------------------
# Field catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportField:
    key: str
    label: str
    group: str
    aliases: tuple = ()
    help: str = ""
    example: str = ""


IMPORT_FIELDS = [
    # Student
    ImportField("full_name", "Full name (one column)", "Student",
                ("name", "names", "full name", "student name", "learner name", "pupil name", "name of student",
                 "student names", "learners name", "candidate name"),
                "Use when first and last names are in one column.", ""),
    ImportField("first_name", "First name", "Student",
                ("first name", "firstname", "given name", "given names", "fname"), "", "John"),
    ImportField("middle_name", "Middle name", "Student", ("middle name", "middlename", "other name"), "", "Paul"),
    ImportField("last_name", "Last name / surname", "Student",
                ("last name", "lastname", "surname", "family name", "lname"), "", "Mukasa"),
    ImportField("gender", "Gender", "Student", ("gender", "sex", "m/f"), "M or F", "M"),
    ImportField("date_of_birth", "Date of birth", "Student",
                ("date of birth", "dob", "birth date", "birthdate", "d.o.b", "date of birth dd/mm/yyyy"),
                "Day/month/year, e.g. 14/03/2011", "14/03/2011"),
    ImportField("residence", "Residence (Day/Boarding)", "Student",
                ("residence", "section", "boarding/day", "day/boarding", "boarding status", "residential status",
                 "day or boarding"), "DAY or BOARDING", "DAY"),
    ImportField("admission_number", "Admission number", "Student",
                ("admission number", "admission no", "adm no", "adm number", "admn no", "admin no", "reg no",
                 "registration number", "registration no", "student number", "student no", "student id",
                 "learner number"), "Kept exactly as written. Leave blank to generate one.", "2345"),
    ImportField("admission_date", "Admission date", "Student",
                ("admission date", "date of admission", "date admitted", "date joined", "joining date"), "", "02/02/2021"),
    ImportField("lin", "LIN (Learner ID)", "Student",
                ("lin", "learner id", "learner identification number", "lin number", "lin no"), "", ""),
    ImportField("uneb_index_number", "UNEB index number", "Student",
                ("uneb index number", "index number", "index no", "uneb index", "uneb no"), "", ""),
    ImportField("nationality", "Nationality", "Student", ("nationality", "country"), "Defaults to Ugandan", "Ugandan"),
    ImportField("religion", "Religion", "Student", ("religion", "religious affiliation", "denomination"), "", "Catholic"),
    ImportField("place_of_birth", "Place of birth", "Student", ("place of birth", "birth place", "home district",
                                                               "district of birth"), "", ""),
    ImportField("previous_school", "Previous school", "Student", ("previous school", "former school", "last school"), "", ""),
    ImportField("previous_class", "Previous class", "Student", ("previous class", "former class"), "", ""),
    # Placement
    ImportField("class_name", "Class", "Placement",
                ("class", "current class", "grade", "class name", "form"), "As named in the system, e.g. S1 or P.5", "S1"),
    ImportField("stream_name", "Stream", "Placement", ("stream", "stream name", "section name"), "", "East"),
    # Parents
    ImportField("parent1_name", "Parent/guardian 1 name", "Parents",
                ("parent name", "parent/guardian name", "parent / guardian", "parent/guardian", "guardian name",
                 "guardian", "parent", "father name", "father's name", "fathers name", "name of parent",
                 "parent 1 name", "parent1 name"), "", "Mukasa Peter"),
    ImportField("parent1_phone", "Parent/guardian 1 phone", "Parents",
                ("parent phone", "parent contact", "parent's contact", "parents contact", "parent tel",
                 "guardian phone", "guardian contact", "father phone", "father's contact", "fathers contact",
                 "father contact", "parent 1 phone", "parent1 phone", "telephone", "tel", "phone", "contact",
                 "phone number", "tel no", "contact number"),
                "Any format: 0772..., 772..., +256772...", "0772123456"),
    ImportField("parent1_relationship", "Parent/guardian 1 relationship", "Parents",
                ("relationship", "parent relationship", "relation", "parent 1 relationship"), "Father, Mother, Guardian...", "Father"),
    ImportField("parent1_email", "Parent/guardian 1 email", "Parents",
                ("parent email", "email", "guardian email", "father email", "parent 1 email"), "", ""),
    ImportField("parent2_name", "Parent/guardian 2 name", "Parents",
                ("mother name", "mother's name", "mothers name", "parent 2 name", "parent2 name", "second parent"), "", "Nakato Jane"),
    ImportField("parent2_phone", "Parent/guardian 2 phone", "Parents",
                ("mother phone", "mother's contact", "mothers contact", "mother contact", "parent 2 phone",
                 "parent2 phone"), "", "0701234567"),
    ImportField("parent2_relationship", "Parent/guardian 2 relationship", "Parents",
                ("parent 2 relationship", "parent2 relationship"), "", "Mother"),
    ImportField("parent2_email", "Parent/guardian 2 email", "Parents", ("mother email", "parent 2 email"), "", ""),
    # Emergency
    ImportField("emergency_contact_name", "Emergency contact name", "Emergency contact",
                ("emergency contact name", "emergency contact", "next of kin", "next of kin name"),
                "Leave blank to use parent 1", ""),
    ImportField("emergency_contact_phone", "Emergency contact phone", "Emergency contact",
                ("emergency contact phone", "emergency phone", "next of kin phone", "next of kin contact"), "", ""),
    ImportField("emergency_contact_relationship", "Emergency contact relationship", "Emergency contact",
                ("emergency contact relationship", "next of kin relationship"), "", ""),
    # Fees
    ImportField("opening_balance", "Fees balance brought forward", "Fees",
                ("opening balance", "fees balance", "fee balance", "balance", "balance b/f", "bal b/f", "arrears",
                 "outstanding", "outstanding balance", "amount owed"),
                "Amount owed. Use a negative number for money paid in advance.", "150000"),
]

FIELD_BY_KEY = {f.key: f for f in IMPORT_FIELDS}
FIELD_KEYS = [f.key for f in IMPORT_FIELDS]

# The order of columns in the downloadable template
TEMPLATE_COLUMNS = [
    "first_name", "middle_name", "last_name", "gender", "date_of_birth", "residence",
    "class_name", "stream_name", "admission_number", "admission_date", "lin", "uneb_index_number",
    "parent1_name", "parent1_phone", "parent1_relationship", "parent1_email",
    "parent2_name", "parent2_phone", "parent2_relationship",
    "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship",
    "nationality", "religion", "place_of_birth", "previous_school", "previous_class", "opening_balance",
]
TEMPLATE_REQUIRED = {"first_name", "last_name", "gender", "date_of_birth", "residence", "class_name"}
TEMPLATE_SHEET = "Students Upload"

PARENT_RELATIONSHIPS = dict(Parent.RELATIONSHIP_CHOICES)


# ---------------------------------------------------------------------------
# Value cleaners
# ---------------------------------------------------------------------------

def normalize_header(value):
    text = str(value or "").strip().lower()
    text = text.replace("_", " ")
    text = re.sub(r"[^a-z0-9/' ]+", " ", text)
    text = text.replace("'", "")
    return re.sub(r"\s+", " ", text).strip()


def clean_text(value, max_length=None):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    if text.lower() in ("none", "nan", "n/a", "na", "-", "nil"):
        return ""
    if max_length:
        text = text[:max_length]
    return text


def clean_code(value):
    """Admission numbers, LINs, index numbers: Excel may turn 2345 into 2345.0."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return clean_text(value)


EXCEL_EPOCH = date(1899, 12, 30)
DATE_FORMATS = [
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%B-%Y", "%b %d %Y", "%B %d %Y", "%d %b, %Y", "%B %d, %Y",
    "%m/%d/%Y",
]


def parse_date(value):
    """Return (date or None, error or '')."""
    if value is None or value == "":
        return None, ""
    if isinstance(value, datetime):
        return value.date(), ""
    if isinstance(value, date):
        return value, ""
    if isinstance(value, (int, float)):
        # Excel serial number (e.g. 40617 = 15/03/2011)
        if 1000 < value < 80000:
            return EXCEL_EPOCH + timedelta(days=int(value)), ""
        return None, f"'{value}' is not a valid date."
    text = clean_text(value)
    if not text:
        return None, ""
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)
    text = text.split(" 00:00")[0].strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date(), ""
        except ValueError:
            continue
    return None, f"'{text}' is not a date we can read. Use day/month/year, e.g. 14/03/2011."


def normalize_gender(value):
    text = clean_text(value).upper().replace(".", "")
    if text in ("M", "MALE", "BOY", "MALES"):
        return "M"
    if text in ("F", "FEMALE", "GIRL", "FEMALES"):
        return "F"
    return ""


def normalize_residence(value):
    text = re.sub(r"[^A-Z]", "", clean_text(value).upper())
    if text in ("BOARDING", "B", "BOARDER", "BOARDERS", "BOARD", "BOARDINGSTUDENT"):
        return "BOARDING"
    if text in ("DAY", "D", "DAYSCHOLAR", "DAYSCHOLARS", "DAYSTUDENT", "DAYS", "DAYSCHOOL"):
        return "DAY"
    return ""


def normalize_relationship(value, header_hint=""):
    text = clean_text(value).upper()
    text = {"DAD": "FATHER", "MUM": "MOTHER", "MOM": "MOTHER", "GRANDMA": "GRANDMOTHER",
            "GRANDPA": "GRANDFATHER", "GUARDIAN/PARENT": "GUARDIAN", "SPONSOR": "GUARDIAN",
            "BROTHER": "OTHER", "SISTER": "OTHER"}.get(text, text)
    if text in PARENT_RELATIONSHIPS:
        return text
    if text:
        return "OTHER"
    hint = (header_hint or "").lower()
    if "father" in hint:
        return "FATHER"
    if "mother" in hint:
        return "MOTHER"
    return "GUARDIAN"


def normalize_phone(value):
    """
    Uganda-aware phone normalisation.
    Returns (primary, alternate, error). Excel often strips the leading 0 (772123456)
    or stores the number as a float (772123456.0); both are recovered.
    """
    if value is None or value == "":
        return "", "", ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    parts = [p for p in re.split(r"[/,;&]|\bor\b|\band\b", text) if p.strip()]
    numbers = []
    for part in parts:
        has_plus = part.strip().startswith("+")
        digits = re.sub(r"\D", "", part)
        if not digits:
            continue
        if has_plus or digits.startswith("256"):
            if digits.startswith("256") and len(digits) == 12:
                numbers.append("+" + digits)
                continue
            if has_plus and 8 <= len(digits) <= 15 and not digits.startswith("256"):
                numbers.append("+" + digits)  # foreign number, keep as is
                continue
        elif len(digits) == 10 and digits.startswith("0"):
            numbers.append("+256" + digits[1:])
            continue
        elif len(digits) == 9 and digits[0] in "234567":
            numbers.append("+256" + digits)
            continue
        return "", "", f"'{text}' does not look like a valid phone number."
    if not numbers:
        return "", "", f"'{text}' does not look like a valid phone number."
    return numbers[0], (numbers[1] if len(numbers) > 1 else ""), ""


def phone_key(value):
    primary, _alt, _err = normalize_phone(value)
    return primary


def parse_amount(value):
    """Return (Decimal or None, error)."""
    if value is None or value == "":
        return None, ""
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)).quantize(Decimal("0.01")), ""
    text = clean_text(value).upper().replace("UGX", "").replace("USH", "").replace("SHS", "").strip()
    if not text:
        return None, ""
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace(" ", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None, f"'{value}' is not a valid amount."
    if negative:
        amount = -amount
    return amount.quantize(Decimal("0.01")), ""


def split_full_name(full, order="FIRST_LAST"):
    """Return (first, middle, last)."""
    text = clean_text(full)
    if "," in text:  # "MUKASA, John Paul" always means surname first
        surname, rest = [p.strip() for p in text.split(",", 1)]
        rest_parts = rest.split()
        return (rest_parts[0] if rest_parts else ""), " ".join(rest_parts[1:]), surname
    parts = text.split()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if order == "SURNAME_FIRST":
        return parts[1], " ".join(parts[2:]), parts[0]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def split_person_name(full):
    """Parent names: first word = first name, rest = last name."""
    parts = clean_text(full).replace(",", " ").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ---------------------------------------------------------------------------
# Class / stream matching
# ---------------------------------------------------------------------------

_WORD_NUMBERS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
}


def class_key(value):
    """'Senior One', 'S.1', 's 1', 'S1' all become 's1'."""
    text = clean_text(value).lower()
    text = re.sub(r"\bsenior\b", "s", text)
    text = re.sub(r"\bprimary\b", "p", text)
    text = re.sub(r"\bform\b", "s", text)
    words = re.split(r"[\s.\-_/]+", text)
    words = [_WORD_NUMBERS.get(w, w) if i > 0 else w for i, w in enumerate(words)]
    return re.sub(r"[^a-z0-9]", "", "".join(words))


def stream_key(value):
    return re.sub(r"[^a-z0-9]", "", clean_text(value).lower())


class ClassIndex:
    """Looks up a school's classes and streams by loosely written names."""

    def __init__(self, school, level=None):
        qs = Class.objects.filter(school=school).select_related("level").prefetch_related("streams")
        if level is not None:
            qs = qs.filter(level=level)
        self.by_key = {}
        self.streams = {}
        for cls in qs:
            for name in {cls.name, cls.short_name}:
                key = class_key(name)
                if key:
                    self.by_key.setdefault(key, set()).add(cls)
            self.streams[cls.pk] = {stream_key(s.name): s for s in cls.streams.all()}

    def find_class(self, text):
        """Return (class or None, error)."""
        key = class_key(text)
        if not key:
            return None, ""
        found = self.by_key.get(key, set())
        if len(found) == 1:
            return next(iter(found)), ""
        if len(found) > 1:
            names = ", ".join(sorted(str(c) for c in found))
            return None, f"Class '{text}' matches more than one class ({names}). Choose a level on the upload page."
        return None, f"Class '{text}' was not found in this school."

    def find_stream(self, cls, text):
        key = stream_key(text)
        if not key:
            return None, ""
        stream = self.streams.get(cls.pk, {}).get(key)
        if stream:
            return stream, ""
        return None, f"Stream '{text}' was not found in {cls.name}."

    def resolve(self, class_text, stream_text):
        """Also understands 'S1 East' written in the class column."""
        cls, error = self.find_class(class_text)
        if cls is None and class_text and not stream_text and " " in clean_text(class_text):
            head, tail = clean_text(class_text).rsplit(" ", 1)
            alt_cls, _ = self.find_class(head)
            if alt_cls:
                alt_stream, _ = self.find_stream(alt_cls, tail)
                if alt_stream:
                    return alt_cls, alt_stream, ""
        if cls is None:
            return None, None, error
        stream, s_error = self.find_stream(cls, stream_text)
        return cls, stream, s_error


# ---------------------------------------------------------------------------
# Reading the workbook
# ---------------------------------------------------------------------------

def _alias_lookup():
    lookup = {}
    for f in IMPORT_FIELDS:
        lookup.setdefault(normalize_header(f.key), f.key)
        lookup.setdefault(normalize_header(f.label), f.key)
        for alias in f.aliases:
            lookup.setdefault(normalize_header(alias), f.key)
    return lookup


ALIAS_LOOKUP = _alias_lookup()


def open_workbook(path_or_file):
    return load_workbook(path_or_file, read_only=True, data_only=True)


def choose_sheet(wb, preferred=""):
    if preferred and preferred in wb.sheetnames:
        return wb[preferred]
    if TEMPLATE_SHEET in wb.sheetnames:
        return wb[TEMPLATE_SHEET]
    for name in wb.sheetnames:  # skip instruction / lookup sheets
        if name.lower() not in ("instructions", "classes", "lists"):
            return wb[name]
    return wb.worksheets[0]


def detect_header_row(ws, scan_rows=15):
    """
    Schools' registers often start with a title such as 'ST. MARY'S SS - S1 REGISTER 2026'.
    The header row is the one (among the first rows) whose cells best match known column names.
    """
    best_row, best_score, best_values = 1, -1, []
    for idx, row in enumerate(ws.iter_rows(min_row=1, max_row=scan_rows, values_only=True), start=1):
        values = [clean_text(v) for v in row]
        filled = [v for v in values if v]
        if len(filled) < 2:
            continue
        matches = sum(1 for v in filled if normalize_header(v) in ALIAS_LOOKUP)
        score = matches * 10 + len(filled)
        if score > best_score:
            best_row, best_score, best_values = idx, score, values
    while best_values and not best_values[-1]:
        best_values.pop()
    return best_row, best_values


def read_sheet_structure(path_or_file, preferred_sheet=""):
    """Return dict(sheet_names, sheet_name, header_row, headers)."""
    wb = open_workbook(path_or_file)
    try:
        ws = choose_sheet(wb, preferred_sheet)
        header_row, headers = detect_header_row(ws)
        headers = unique_headers(headers)
        return {
            "sheet_names": list(wb.sheetnames),
            "sheet_name": ws.title,
            "header_row": header_row,
            "headers": headers,
        }
    finally:
        wb.close()


def read_headers_at(path_or_file, sheet_name, header_row):
    """Headers from an explicit row (when the admin corrects the detected header row)."""
    wb = open_workbook(path_or_file)
    try:
        ws = choose_sheet(wb, sheet_name)
        cells = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True), ())
        texts = [clean_text(v) for v in cells]
        while texts and not texts[-1]:
            texts.pop()
        return unique_headers(texts)
    finally:
        wb.close()


def sample_rows(path_or_file, sheet_name, header_row, headers, limit=3):
    """First few data rows, shown on the column-matching page so the admin can check."""
    wb = open_workbook(path_or_file)
    try:
        ws = choose_sheet(wb, sheet_name)
        out = []
        for cells in ws.iter_rows(min_row=header_row + 1, max_row=header_row + 30, values_only=True):
            if not cells or not any(clean_text(c) for c in cells):
                continue
            out.append({h: clean_text(_json_safe(cells[i])) if i < len(cells) else "" for i, h in enumerate(headers)})
            if len(out) >= limit:
                break
        return out
    finally:
        wb.close()


def unique_headers(headers):
    """Blank headers become 'Column 5'; repeated headers get ' (2)'."""
    seen = {}
    out = []
    for i, h in enumerate(headers, start=1):
        h = clean_text(h) or f"Column {i}"
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        out.append(h)
    return out


def auto_map_columns(headers):
    """Return {field_key: header} for headers we recognise."""
    mapping = {}
    for header in headers:
        key = ALIAS_LOOKUP.get(normalize_header(header))
        if key and key not in mapping:
            mapping[key] = header
    # A register with "Father's contact" and "Mother's contact" gets both parents.
    if "full_name" in mapping and "first_name" in mapping and "last_name" in mapping:
        mapping.pop("full_name")
    return mapping


def mapping_problems(mapping, batch):
    """Errors that stop staging. ``batch`` supplies defaults (class, residence...)."""
    problems = []
    has_split = "first_name" in mapping and "last_name" in mapping
    if not has_split and "full_name" not in mapping:
        problems.append("Match either a full name column, or both first name and last name columns.")
    if "gender" not in mapping:
        problems.append("Match the gender column.")
    if "residence" not in mapping and not batch.default_residence:
        problems.append("Match the residence column, or choose a default residence.")
    if "date_of_birth" not in mapping and not batch.allow_missing_dob:
        problems.append("Match the date of birth column, or allow missing dates of birth.")
    if "class_name" not in mapping and not batch.current_class_id:
        problems.append("Match the class column, or choose one class for the whole file.")
    used = [h for h in mapping.values() if h]
    if len(used) != len(set(used)):
        problems.append("Each file column can only be matched to one field.")
    return problems


# ---------------------------------------------------------------------------
# Row evaluation (shared by staging and by editing a row on the review page)
# ---------------------------------------------------------------------------

@dataclass
class SchoolContext:
    """Everything needed to validate rows without one query per row."""
    school: object
    class_index: ClassIndex
    by_admission: dict = field(default_factory=dict)
    by_lin: dict = field(default_factory=dict)
    by_name_dob: dict = field(default_factory=dict)
    by_name: dict = field(default_factory=dict)

    @classmethod
    def build(cls, batch):
        ctx = cls(school=batch.school, class_index=ClassIndex(batch.school, batch.level))
        students = Student.objects.filter(school=batch.school).only(
            "id", "name", "first_name", "middle_name", "last_name", "date_of_birth", "admission_number", "lin",
        )
        for s in students:
            if s.admission_number:
                ctx.by_admission[s.admission_number.strip().upper()] = s
            if s.lin:
                ctx.by_lin[s.lin.strip().upper()] = s
            key = normalize_student_name(s.first_name, s.middle_name or "", s.last_name)
            ctx.by_name.setdefault(key, s)
            if s.date_of_birth:
                ctx.by_name_dob.setdefault((key, s.date_of_birth), s)
        return ctx


def extract_row_values(raw, batch):
    """Turn {header: cell} into cleaned field values plus parse errors/warnings."""
    mapping = batch.column_mapping or {}

    def cell(key):
        header = mapping.get(key)
        return raw.get(header) if header else None

    errors, warnings = [], []
    values = {}

    first = clean_text(cell("first_name"), 100)
    middle = clean_text(cell("middle_name"), 100)
    last = clean_text(cell("last_name"), 100)
    if not (first and last) and cell("full_name"):
        f2, m2, l2 = split_full_name(cell("full_name"), batch.name_order)
        first, middle, last = first or f2, middle or m2, last or l2
    values.update(first_name=first[:100], middle_name=middle[:100], last_name=last[:100])

    dob, err = parse_date(cell("date_of_birth"))
    if err:
        errors.append(f"Date of birth: {err}")
    values["date_of_birth"] = dob

    adm_date, err = parse_date(cell("admission_date"))
    if err:
        warnings.append(f"Admission date ignored: {err}")
        adm_date = None
    values["admission_date"] = adm_date

    raw_gender = cell("gender")
    values["gender"] = normalize_gender(raw_gender)
    raw_res = cell("residence")
    values["residence"] = normalize_residence(raw_res) or (batch.default_residence if not clean_text(raw_res) else "")
    values["_raw_gender"] = clean_text(raw_gender)
    values["_raw_residence"] = clean_text(raw_res)

    values["admission_number"] = clean_code(cell("admission_number"))
    values["lin"] = clean_code(cell("lin")).upper()
    values["uneb_index_number"] = clean_code(cell("uneb_index_number"))
    values["nationality"] = clean_text(cell("nationality"), 50) or "Ugandan"
    values["religion"] = clean_text(cell("religion"), 50)
    values["place_of_birth"] = clean_text(cell("place_of_birth"), 100)
    values["previous_school"] = clean_text(cell("previous_school"), 200)
    values["previous_class"] = clean_text(cell("previous_class"), 50)
    values["class_name"] = clean_text(cell("class_name"), 100)
    values["stream_name"] = clean_text(cell("stream_name"), 100)

    for n in ("1", "2"):
        name = clean_text(cell(f"parent{n}_name"), 150)
        phone, alt, err = normalize_phone(cell(f"parent{n}_phone"))
        if err:
            warnings.append(f"Parent {n} phone not saved: {err}")
        rel = normalize_relationship(cell(f"parent{n}_relationship"), mapping.get(f"parent{n}_name", "")) \
            if (name or phone) else ""
        email = clean_text(cell(f"parent{n}_email"), 254).lower()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            warnings.append(f"Parent {n} email '{email}' is not valid and will be ignored.")
            email = ""
        values.update({
            f"parent{n}_name": name, f"parent{n}_phone": phone, f"parent{n}_alt_phone": alt,
            f"parent{n}_relationship": rel, f"parent{n}_email": email,
        })

    em_phone, _alt, err = normalize_phone(cell("emergency_contact_phone"))
    if err:
        warnings.append(f"Emergency phone not saved: {err}")
    values["emergency_contact_name"] = clean_text(cell("emergency_contact_name"), 100)
    values["emergency_contact_phone"] = em_phone
    values["emergency_contact_relationship"] = clean_text(cell("emergency_contact_relationship"), 50)

    amount, err = parse_amount(cell("opening_balance"))
    if err:
        errors.append(f"Fees balance: {err}")
    values["opening_balance"] = amount

    return values, errors, warnings


def validate_row(row, batch, ctx):
    """
    Check one staged row (in place). Sets is_valid, validation_errors, warnings,
    resolved class/stream and DB duplicate flags. In-file duplicates are handled by the caller.
    """
    # Keep parse errors found while reading cells; everything else is recomputed here.
    errors = [e for e in (row.validation_errors.split(" | ") if row.validation_errors else [])
              if e.startswith(("Date of birth:", "Fees balance:"))]
    warnings = [w for w in (row.warnings.split(" | ") if row.warnings else [])
                if w and not w.startswith(("Age ", "No parent", "Unreadable date of birth"))]
    if batch.allow_missing_dob:
        for e in [e for e in errors if e.startswith("Date of birth:")]:
            errors.remove(e)
            warnings.append("Unreadable date of birth left blank. " + e.split(":", 1)[1].strip())

    if not row.first_name:
        errors.append("First name is required.")
    if not row.last_name:
        errors.append("Last name is required (only one name was given)." if row.first_name else "Last name is required.")
    if row.gender not in ("M", "F"):
        raw = clean_text((row.raw_data or {}).get((batch.column_mapping or {}).get("gender", ""), ""))
        errors.append(f"Gender '{raw}' is not M or F." if raw and not row.pk else "Gender is required (M or F).")
    if row.residence not in ("BOARDING", "DAY"):
        errors.append("Residence must be DAY or BOARDING.")
    if not row.date_of_birth:
        if not batch.allow_missing_dob and not any(e.startswith("Date of birth:") for e in errors):
            errors.append("Date of birth is required.")
    else:
        today = timezone.now().date()
        if row.date_of_birth > today:
            errors.append("Date of birth is in the future.")
        else:
            age = today.year - row.date_of_birth.year
            if age < 2 or age > 30:
                warnings.append(f"Age {age} looks unusual. Check the date of birth.")
    if row.opening_balance and not (
        batch.fee_category_id and batch.balance_academic_year_id and batch.balance_term_id
    ):
        errors.append("This row has a fees balance. Choose the fee category, academic year and term for "
                      "balances on the Match Columns page, or unmatch the balance column.")
    if len(row.admission_number) > 30:
        errors.append("Admission number is longer than 30 characters.")

    # Placement
    class_text = row.class_name
    stream_text = row.stream_name
    row.resolved_class = None
    row.resolved_stream = None
    if class_text:
        cls, stream, err = ctx.class_index.resolve(class_text, stream_text)
        if err:
            errors.append(err)
        row.resolved_class, row.resolved_stream = cls, stream
    elif batch.current_class_id:
        row.resolved_class = batch.current_class
        row.resolved_stream = batch.stream
        if stream_text:
            stream, err = ctx.class_index.find_stream(batch.current_class, stream_text)
            if err:
                errors.append(err)
            row.resolved_stream = stream
    else:
        errors.append("Class is required.")

    if not (row.parent1_name or row.parent1_phone or row.parent2_name or row.parent2_phone
            or row.emergency_contact_phone):
        warnings.append("No parent or emergency contact. Parents will not receive SMS for this learner.")

    # Duplicates already in the school
    row.is_possible_duplicate_in_db = False
    row.duplicate_db_note = ""
    row.matched_student = None
    match, note = None, ""
    if row.admission_number and row.admission_number.upper() in ctx.by_admission:
        match = ctx.by_admission[row.admission_number.upper()]
        note = f"Admission number {row.admission_number} already belongs to {match.name}."
    elif row.lin and row.lin.upper() in ctx.by_lin:
        match = ctx.by_lin[row.lin.upper()]
        note = f"LIN {row.lin} already belongs to {match.name} ({match.admission_number})."
    else:
        key = normalize_student_name(row.first_name, row.middle_name or "", row.last_name)
        if row.date_of_birth and (key, row.date_of_birth) in ctx.by_name_dob:
            match = ctx.by_name_dob[(key, row.date_of_birth)]
            note = f"Same name and date of birth as {match.name} ({match.admission_number})."
        elif not row.date_of_birth and key in ctx.by_name:
            match = ctx.by_name[key]
            note = f"Same name as {match.name} ({match.admission_number}). No date of birth to compare."
    if match:
        row.is_possible_duplicate_in_db = True
        row.duplicate_db_note = note[:255]
        row.matched_student = match

    row.is_valid = not errors
    row.validation_errors = " | ".join(errors)
    row.warnings = " | ".join(warnings)
    return row


def default_action(row):
    if not row.is_valid or row.is_duplicate_in_file:
        return "REVIEW"
    if row.is_possible_duplicate_in_db:
        # An admission number or LIN match is almost surely the same learner.
        strong = row.duplicate_db_note.startswith(("Admission number", "LIN"))
        return "SKIP" if strong else "REVIEW"
    return "CREATE"


def file_duplicate_keys(row):
    keys = []
    if row.admission_number:
        keys.append(("adm", row.admission_number.upper()))
    if row.lin:
        keys.append(("lin", row.lin.upper()))
    name = normalize_student_name(row.first_name, row.middle_name or "", row.last_name)
    if name and row.date_of_birth:
        keys.append(("name_dob", name, row.date_of_birth))
    return keys


_KEY_LABEL = {"adm": "admission number", "lin": "LIN", "name_dob": "name and date of birth"}


def apply_values(row, values, parse_errors, parse_warnings):
    for key, val in values.items():
        if not key.startswith("_"):
            setattr(row, key, val)
    row.validation_errors = " | ".join(parse_errors)
    row.warnings = " | ".join(parse_warnings)
    row.refresh_names()


# ---------------------------------------------------------------------------
# Staging (file -> StudentBulkUploadRow)
# ---------------------------------------------------------------------------

def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _set_progress(batch, done, total=None):
    from mainApp.models.student import StudentBulkUploadBatch
    updates = {"progress_done": done}
    if total is not None:
        updates["progress_total"] = total
    StudentBulkUploadBatch.objects.filter(pk=batch.pk).update(**updates)


def stage_batch(batch):
    """Read the file into staging rows. Raises ValueError with a readable message on bad files."""
    from mainApp.models.student import StudentBulkUploadBatch

    problems = mapping_problems(batch.column_mapping or {}, batch)
    if problems:
        raise ValueError(" ".join(problems))

    wb = open_workbook(batch.file.path)
    try:
        ws = choose_sheet(wb, batch.sheet_name)
        header_cells = next(ws.iter_rows(min_row=batch.header_row, max_row=batch.header_row, values_only=True), ())
        header_texts = [clean_text(v) for v in header_cells]
        while header_texts and not header_texts[-1]:
            header_texts.pop()
        headers = unique_headers(header_texts)
        missing = [h for h in (batch.column_mapping or {}).values() if h and h not in headers]
        if missing:
            raise ValueError(f"These columns are no longer in the file: {', '.join(missing)}. Upload the file again.")

        approx_total = max((ws.max_row or 0) - batch.header_row, 0)
        _set_progress(batch, 0, approx_total)

        ctx = SchoolContext.build(batch)
        batch.rows.all().delete()

        seen = {}
        pending = []
        counts = {"total": 0, "valid": 0, "invalid": 0, "dup_file": 0, "dup_db": 0}

        for excel_row_no, cells in enumerate(
            ws.iter_rows(min_row=batch.header_row + 1, values_only=True), start=batch.header_row + 1
        ):
            if not cells or not any(clean_text(c) for c in cells):
                continue
            raw = {h: _json_safe(cells[i]) if i < len(cells) else None for i, h in enumerate(headers)}
            values, perrs, pwarns = extract_row_values(raw, batch)
            # Skip repeated header lines and "TOTAL" footer rows common in registers
            joined = " ".join(clean_text(v) for v in raw.values() if v is not None).lower()
            if values["first_name"] and normalize_header(values["first_name"]) in ALIAS_LOOKUP and not values["gender"]:
                continue
            if joined.startswith(("total", "grand total")):
                continue

            row = StudentBulkUploadRow(batch=batch, row_number=excel_row_no, raw_data=raw)
            if batch.created_by_id:
                row.created_by_id = batch.created_by_id
            apply_values(row, values, perrs, pwarns)
            validate_row(row, batch, ctx)

            for key in file_duplicate_keys(row):
                if key in seen:
                    row.is_duplicate_in_file = True
                    row.duplicate_file_note = f"Same {_KEY_LABEL[key[0]]} as row {seen[key]}."
                    break
            for key in file_duplicate_keys(row):
                seen.setdefault(key, excel_row_no)

            row.action = default_action(row)
            counts["total"] += 1
            counts["valid" if row.is_valid else "invalid"] += 1
            counts["dup_file"] += int(row.is_duplicate_in_file)
            counts["dup_db"] += int(row.is_possible_duplicate_in_db)
            pending.append(row)

            if len(pending) >= STAGE_CHUNK:
                StudentBulkUploadRow.objects.bulk_create(pending)
                pending = []
                _set_progress(batch, counts["total"])
        if pending:
            StudentBulkUploadRow.objects.bulk_create(pending)
    finally:
        wb.close()

    if counts["total"] == 0:
        raise ValueError("No learner rows were found under the header row.")

    StudentBulkUploadBatch.objects.filter(pk=batch.pk).update(
        total_rows=counts["total"], valid_rows=counts["valid"], invalid_rows=counts["invalid"],
        duplicate_file_rows=counts["dup_file"], duplicate_db_rows=counts["dup_db"],
        progress_done=counts["total"], progress_total=counts["total"],
        status="UPLOADED", error_message="",
    )
    batch.refresh_from_db()
    return batch


def refresh_batch_counts(batch):
    from mainApp.models.student import StudentBulkUploadBatch
    rows = batch.rows.all()
    StudentBulkUploadBatch.objects.filter(pk=batch.pk).update(
        total_rows=rows.count(),
        valid_rows=rows.filter(is_valid=True).count(),
        invalid_rows=rows.filter(is_valid=False).count(),
        duplicate_file_rows=rows.filter(is_duplicate_in_file=True).count(),
        duplicate_db_rows=rows.filter(is_possible_duplicate_in_db=True).count(),
        imported_rows=rows.filter(is_imported=True).count(),
        failed_rows=rows.exclude(import_error="").filter(is_imported=False).count(),
    )
    batch.refresh_from_db()


def revalidate_single_row(row):
    """After an admin edits a row on the review page. Edited values are already clean."""
    batch = row.batch
    ctx = SchoolContext.build(batch)
    row.validation_errors = ""
    row.warnings = ""
    row.import_error = ""
    row.refresh_names()
    validate_row(row, batch, ctx)

    row.is_duplicate_in_file = False
    row.duplicate_file_note = ""
    my_keys = set(file_duplicate_keys(row))
    if my_keys:
        for other in batch.rows.exclude(pk=row.pk).filter(row_number__lt=row.row_number):
            common = my_keys & set(file_duplicate_keys(other))
            if common:
                row.is_duplicate_in_file = True
                row.duplicate_file_note = f"Same {_KEY_LABEL[next(iter(common))[0]]} as row {other.row_number}."
                break
    row.action = default_action(row)
    row.save()
    refresh_batch_counts(batch)
    return row


# ---------------------------------------------------------------------------
# Import (StudentBulkUploadRow -> Student, Parent, fees)
# ---------------------------------------------------------------------------

class ParentResolver:
    """Finds a school's existing parent by phone (siblings share parents) or creates one,
    then links them to the student with this child's relationship and roles."""

    def __init__(self, school):
        self.school = school
        self.by_phone = {}
        for p in Parent.objects.filter(school=school).only("id", "phone", "alternate_phone", "relationship"):
            for number in (p.phone, p.alternate_phone):
                key = phone_key(number)
                if key:
                    self.by_phone.setdefault(key, p)
        self.created = 0
        self.linked = 0

    def _link(self, student, parent, relationship, primary):
        """Primary / fee payer / emergency contact now live on the per-child link."""
        if not getattr(student, "pk", None):
            return  # student not saved yet; the caller's parents.add() will link with defaults
        link_guardian(
            student, parent,
            relationship=relationship or parent.relationship or "GUARDIAN",
            is_primary=primary,
            is_fee_payer=primary,
            is_emergency_contact=primary,
        )

    def get_or_create(self, name, phone, alt_phone, relationship, email, student, primary):
        if not (name or phone):
            return None, ""
        if phone and phone in self.by_phone:
            parent = self.by_phone[phone]
            self._link(student, parent, relationship, primary)
            self.linked += 1
            return parent, ""
        note = ""
        first, last = split_person_name(name)
        if not first:
            first, last = "Parent of", student.name
            note = "Parent added without a name (phone only)."
        if email and Parent.objects.filter(email__iexact=email).exists():
            note = f"Parent email {email} is already used by another parent, so it was left out."
            email = ""
        parent = Parent.objects.create(
            school=self.school,
            first_name=first[:50],
            last_name=(last or "")[:50],
            relationship=relationship or "GUARDIAN",
            phone=phone,
            alternate_phone=alt_phone or None,
            email=email or None,
            address="",
        )
        if phone:
            self.by_phone[phone] = parent
        self._link(student, parent, relationship, primary)
        self.created += 1
        return parent, note

def _create_student(row, batch, user):
    cls = row.resolved_class
    emergency_name = row.emergency_contact_name or row.parent1_name or row.parent2_name
    emergency_phone = row.emergency_contact_phone or row.parent1_phone or row.parent2_phone
    emergency_rel = row.emergency_contact_relationship
    if not emergency_rel:
        rel_code = row.parent1_relationship if (row.parent1_name or row.parent1_phone) else row.parent2_relationship
        emergency_rel = PARENT_RELATIONSHIPS.get(rel_code, "") if rel_code else ""

    student = Student(
        school=batch.school,
        level=cls.level,
        current_class=cls,
        stream=row.resolved_stream,
        first_name=row.first_name,
        middle_name=row.middle_name or None,
        last_name=row.last_name,
        name=row.full_name,
        date_of_birth=row.date_of_birth,
        gender=row.gender,
        residence=row.residence,
        admission_number=row.admission_number or "",
        admission_date=row.admission_date,
        lin=row.lin,
        uneb_index_number=row.uneb_index_number,
        place_of_birth=row.place_of_birth or None,
        previous_school=row.previous_school or None,
        previous_class=row.previous_class or None,
        emergency_contact_name=(emergency_name or "")[:100],
        emergency_contact_phone=(emergency_phone or "")[:20],
        emergency_contact_relationship=(emergency_rel or "")[:50],
        nationality=row.nationality or "Ugandan",
        religion=row.religion or None,
    )
    if user is not None:
        student.created_by = user
    student.save()
    return student


def _post_balance(row, batch, student):
    from mainApp.models.fees_models import StudentFeeCredit, StudentFeeOpeningBalance
    from mainApp.fee_services import post_opening_balance

    amount = row.opening_balance
    if amount is None or amount == 0:
        return False
    if amount > 0:
        ob = StudentFeeOpeningBalance.objects.create(
            school=batch.school, student=student,
            academic_year=batch.balance_academic_year, term=batch.balance_term,
            fee_category=batch.fee_category, amount=amount,
            notes=f"Brought forward at onboarding import (batch #{batch.pk}, row {row.row_number}).",
        )
        post_opening_balance(ob)
    else:
        credit = abs(amount)
        StudentFeeCredit.objects.create(
            school=batch.school, student=student,
            academic_year=batch.balance_academic_year, term=batch.balance_term,
            amount=credit, balance=credit,
            notes=f"Advance payment brought forward at onboarding import (batch #{batch.pk}, row {row.row_number}).",
        )
    return True


def import_batch(batch, user=None):
    """
    Create students for every valid row marked CREATE. Each row is saved in its own
    savepoint, so one bad row is reported instead of rolling back the whole school.
    Safe to run again: rows already imported are skipped.
    """
    from mainApp.models.student import StudentBulkUploadBatch

    rows = list(
        batch.rows.filter(is_imported=False, is_valid=True, action="CREATE")
        .select_related("resolved_class__level", "resolved_stream", "matched_student")
        .order_by("row_number")
    )
    _set_progress(batch, 0, len(rows))

    ctx = SchoolContext.build(batch)
    parents = ParentResolver(batch.school) if batch.create_parents else None
    created_ids, balances = [], 0

    for i, row in enumerate(rows, start=1):
        try:
            with transaction.atomic():
                if row.admission_number and row.admission_number.upper() in ctx.by_admission:
                    raise ValueError(f"Admission number {row.admission_number} is already used in this school.")
                key = normalize_student_name(row.first_name, row.middle_name or "", row.last_name)
                existing = ctx.by_name_dob.get((key, row.date_of_birth)) if row.date_of_birth else None
                if existing and not row.is_possible_duplicate_in_db:
                    # Someone added this learner after the file was checked.
                    row.action = "SKIP"
                    row.is_possible_duplicate_in_db = True
                    row.matched_student = existing
                    row.duplicate_db_note = f"Skipped: {existing.name} ({existing.admission_number}) was added meanwhile."
                    row.save(update_fields=["action", "is_possible_duplicate_in_db", "matched_student", "duplicate_db_note"])
                    continue

                student = _create_student(row, batch, user)
                notes = []
                if parents is not None:
                    linked = []
                    for n, primary in (("1", True), ("2", False)):
                        parent, note = parents.get_or_create(
                            getattr(row, f"parent{n}_name"), getattr(row, f"parent{n}_phone"),
                            getattr(row, f"parent{n}_alt_phone"), getattr(row, f"parent{n}_relationship"),
                            getattr(row, f"parent{n}_email"), student, primary,
                        )
                        if note:
                            notes.append(note)
                        if parent and parent not in linked:
                            linked.append(parent)
                    if linked:
                        student.parents.add(*linked)
                if _post_balance(row, batch, student):
                    balances += 1

                row.is_imported = True
                row.imported_student = student
                row.import_error = ""
                if notes:
                    row.warnings = " | ".join([w for w in [row.warnings] + notes if w])
                row.save(update_fields=["is_imported", "imported_student", "import_error", "warnings"])

                ctx.by_admission[student.admission_number.upper()] = student
                if key and row.date_of_birth:
                    ctx.by_name_dob[(key, row.date_of_birth)] = student
                created_ids.append(student.pk)
        except Exception as exc:  # report and carry on with the next learner
            logger.exception("Student import batch %s row %s failed", batch.pk, row.row_number)
            StudentBulkUploadRow.objects.filter(pk=row.pk).update(import_error=str(exc)[:1000])
        if i % PROGRESS_EVERY == 0:
            _set_progress(batch, i)

    rows_qs = batch.rows.all()
    StudentBulkUploadBatch.objects.filter(pk=batch.pk).update(
        imported_rows=rows_qs.filter(is_imported=True).count(),
        failed_rows=rows_qs.filter(is_imported=False).exclude(import_error="").count(),
        parents_created=(batch.parents_created + (parents.created if parents else 0)),
        parents_linked=(batch.parents_linked + (parents.linked if parents else 0)),
        balances_posted=batch.balances_posted + balances,
        progress_done=len(rows), status="IMPORTED", finished_at=timezone.now(), error_message="",
    )
    batch.refresh_from_db()

    if batch.generate_accounts and created_ids and user is not None:
        queue_account_generation(batch, user, created_ids)
    return created_ids


def queue_account_generation(batch, user, student_ids):
    """Reuse the existing background job that creates student logins."""
    from mainApp.models.student import StudentBulkUploadBatch
    from mainApp.models.user_jobs import StudentAccountGenerationJob
    from mainApp.tasks.user_creation_tasks import generate_student_accounts_task

    job = StudentAccountGenerationJob.objects.create(
        requested_by=user,
        selection_mode="selected",
        selected_ids=[str(i) for i in student_ids],
        status="PENDING",
        message=f"Created from student import batch #{batch.pk}.",
    )
    StudentBulkUploadBatch.objects.filter(pk=batch.pk).update(accounts_job_id=job.pk)

    def enqueue():
        try:
            result = generate_student_accounts_task.delay(str(job.pk))
            StudentAccountGenerationJob.objects.filter(pk=job.pk).update(celery_task_id=result.id or "")
        except Exception:
            logger.exception("Could not queue account generation for batch %s", batch.pk)
            StudentAccountGenerationJob.objects.filter(pk=job.pk).update(
                status="FAILED", message="Could not reach the background worker. Generate accounts from User Management."
            )

    transaction.on_commit(enqueue)
    return job


# ---------------------------------------------------------------------------
# Excel outputs: template and error rows
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
REQUIRED_FILL = PatternFill("solid", fgColor="C00000")
HEADER_FONT = Font(bold=True, color="FFFFFF")


def build_template_workbook(school):
    wb = Workbook()
    ws = wb.active
    ws.title = TEMPLATE_SHEET

    for col, key in enumerate(TEMPLATE_COLUMNS, start=1):
        c = ws.cell(row=1, column=col, value=key)
        c.font = HEADER_FONT
        c.fill = REQUIRED_FILL if key in TEMPLATE_REQUIRED else HEADER_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.cell(row=2, column=col, value=FIELD_BY_KEY[key].example or None)
        ws.column_dimensions[c.column_letter].width = max(14, len(key) + 4)
    ws.freeze_panes = "A2"

    col_of = {k: i + 1 for i, k in enumerate(TEMPLATE_COLUMNS)}

    def letter(key):
        return ws.cell(row=1, column=col_of[key]).column_letter

    for key, options in (
        ("gender", '"M,F"'),
        ("residence", '"DAY,BOARDING"'),
        ("parent1_relationship", '"Father,Mother,Guardian,Grandfather,Grandmother,Uncle,Aunt,Other"'),
        ("parent2_relationship", '"Father,Mother,Guardian,Grandfather,Grandmother,Uncle,Aunt,Other"'),
    ):
        dv = DataValidation(type="list", formula1=options, allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{letter(key)}2:{letter(key)}5000")
    for key in ("parent1_phone", "parent2_phone", "emergency_contact_phone", "admission_number", "lin"):
        for r in range(2, 5001):
            ws.cell(row=r, column=col_of[key]).number_format = "@"  # keep leading zeros

    # Classes sheet so schools type class names that the system will recognise
    wc = wb.create_sheet("Classes")
    wc.append(["Level", "Class", "Streams"])
    for c in wc[1]:
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    for cls in Class.objects.filter(school=school).select_related("level").prefetch_related("streams"):
        wc.append([cls.level.name, cls.name, ", ".join(s.name for s in cls.streams.all())])
    for col, width in (("A", 30), ("B", 18), ("C", 40)):
        wc.column_dimensions[col].width = width

    wi = wb.create_sheet("Instructions", 0)
    lines = [
        ("Student import template", True),
        ("", False),
        ("Fill the 'Students Upload' sheet, one learner per row. Delete the example in row 2.", False),
        ("Red columns are required. Blue columns are optional.", False),
        ("You can also upload your own register without this template: you will match its columns on the next screen.", False),
        ("", False),
        ("Tips", True),
        ("Class: type it as listed on the 'Classes' sheet (S1, S.1 and Senior One are all understood).", False),
        ("Admission number: keep your existing numbers. Leave blank and the system creates one.", False),
        ("Phone numbers: any format works, e.g. 0772123456, 772123456 or +256772123456.", False),
        ("Two phone numbers in one cell (0772.../0701...): the second is saved as the alternate number.", False),
        ("Parents with the same phone number are created once and linked to all their children.", False),
        ("Fees balance: amount the learner owes. Use a negative number for money paid in advance.", False),
        ("Dates: day/month/year, e.g. 14/03/2011.", False),
        ("", False),
        ("Columns", True),
    ]
    for text, bold in lines:
        wi.append([text])
        if bold:
            wi.cell(row=wi.max_row, column=1).font = Font(bold=True, size=13 if text.startswith("Student") else 11)
    wi.append(["Column", "Required", "What to enter"])
    for c in wi[wi.max_row]:
        c.font = Font(bold=True)
    for key in TEMPLATE_COLUMNS:
        f = FIELD_BY_KEY[key]
        wi.append([key, "Yes" if key in TEMPLATE_REQUIRED else "No", f.help or f.label])
    wi.column_dimensions["A"].width = 32
    wi.column_dimensions["B"].width = 10
    wi.column_dimensions["C"].width = 90
    wb.active = wb.index(ws)
    return wb


def build_error_rows_workbook(batch):
    """Rows that still need fixing, in the school's own column layout, plus an Errors column."""
    wb = Workbook()
    ws = wb.active
    ws.title = TEMPLATE_SHEET
    headers = list(batch.headers or [])
    ws.append(headers + ["Errors to fix"])
    for c in ws[1]:
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
    problem_rows = batch.rows.filter(is_imported=False).exclude(action="SKIP").order_by("row_number")
    for row in problem_rows:
        if row.is_valid and not row.import_error and not row.needs_attention:
            continue
        reasons = [r for r in [row.validation_errors, row.duplicate_file_note, row.duplicate_db_note, row.import_error] if r]
        if not reasons:
            continue
        ws.append([(row.raw_data or {}).get(h) for h in headers] + [" | ".join(reasons)])
    ws.column_dimensions[ws.cell(row=1, column=len(headers) + 1).column_letter].width = 70
    return wb
