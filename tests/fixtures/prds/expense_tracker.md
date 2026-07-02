# Expense Tracker

## Data Models

- Budget: name (string), amount (decimal max_digits=12 decimal_places=2), user (auth user)
- Expense: title (text), amount (decimal), budget (belongs to Budget), notes (long text optional), user (auth user)

## Pages

- Dashboard: budget totals and recent expenses
- Budget List: list budgets
- Expense List: list expenses
- Expense Form: create expenses
