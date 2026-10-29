# Connecting a school to SchoolPay

For the school's bursar or administrator. Takes about 15 minutes.

## What you need from SchoolPay
Email support@schoolpay.co.ug or call 0200 502 140 and ask for:
1. Your **school code** (a number).
2. Your **transactions API password**.
3. (Optional) The **IP addresses of their web hook servers**.

The school must have an active SchoolPay subscription - SchoolPay stops
sending notifications when it expires.

## Step 1 - Make sure the current term is set
Payments are recorded against the school's current term. Without it, they
wait in "Needs attention" until it is set.

## Step 2 - Enter the connection details
Fee Management > SchoolPay Connection. Enter the school code and API password,
tick "Connection switched on", save, then click **Test connection**.

## Step 3 - Link students to their SchoolPay codes
Either:
- **Import codes**: export the student list from SchoolPay, make a two-column
  file (admission number, SchoolPay code) and upload it; or
- In SchoolPay, set each student's registration number to their admission
  number. The system then matches them automatically and saves the code.

## Step 4 - Switch on instant notifications
Copy the address shown under "Instant notifications". In the SchoolPay school
portal, paste it into the web hook settings and enable web hooks.

## Step 5 - Load past payments (optional)
On the SchoolPay Payments page, pick a date range (up to 3 months) and click
"Check SchoolPay now". Every payment is posted once only, however often you sync.

## Daily routine
Open **SchoolPay Payments**. A red number in the sidebar means payments need you:
- **Student not matched** - choose the student (suggestions are shown by name)
  and click "Post to this student". Tick "Save code" so it never happens again.
- **Not posted** - read the reason, fix it (e.g. set the current term), click "Try again".
- **Amount mismatch** - the instant notification disagreed with SchoolPay's own
  records. Click "Correct to SchoolPay amount": the wrong receipt is reversed and
  the right amount posted. Report repeated mismatches - it may mean tampering.

The strip at the top compares today's SchoolPay total with what was posted.
A difference of zero means everything is accounted for.

## Settings explained
- **Strict mode**: payments are only posted after the hourly check confirms the
  amount with SchoolPay. Safest; payments appear up to an hour later.
- **SMS parents**: after each payment the parent gets the receipt number and
  new balance (uses your Africa's Talking credit).
- **Days to re-check**: how far back the hourly check looks (default 3).
