# DECISIONS.md
A unique index on (emp_code, date) makes the database enforce one record per day, so concurrent punch-ins can’t create duplicates.
The history array length is the version number, and updates are guarded so concurrent edits can’t overwrite each other.
Half-day units, integers and Fraction keep rounding exact half-up.
Average work hours is a mean over records, not a mean of per-employee means.
In the trend, the range filter runs after the headcount window so earlier joins still count.
The trend returns 404 for an unknown department, while a filter that matches nothing returns an empty list.