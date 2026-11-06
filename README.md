# Money plan calculator

A small, dependency-free calculator for three questions a student or first-job earner keeps asking:

- What actually lands in my bank account each month, and what is left after bills?
- How long does a savings goal take at different income levels?
- What does a car really cost per month once the loan, insurance, fuel, tires, upkeep and taxes are added up, and what income do the usual lending rules say I need for it?

It also shows what the same monthly amount grows to if invested instead.

Two versions share the same formulas:

- `index.html` + `app.js`: a single-page web app. Open `index.html` in a browser, change any number, and every table updates.
- `money_plan.py`: a command-line version with the same math.

Taxes are 2026 single-filer estimates (federal brackets and standard deduction, Arkansas brackets, FICA). They are estimates, not tax advice.

## Run the web app

Open `index.html`. Because `app.js` is loaded as an ES module, some browsers refuse module scripts over `file://`; if the tables stay blank, serve the folder:

```bash
python3 -m http.server 8000
```

then open http://localhost:8000.

## Run the tests

```bash
node test.mjs
```

The tests check the JavaScript against the Python version and against hand-worked values (take-home pay at several incomes, the Arkansas tax on $24,000, a $44,000 loan at 10% over 72 months, $1,900 a month invested for ten years, a sample budget, and a $55,000 car scenario).

## Command-line version

```bash
python3 money_plan.py budget --income 2500 --rent 650 --utilities 150 --car-payment 350 --insurance 300
python3 money_plan.py timeline --rent 650 --utilities 150 --car-payment 350 --insurance 300 --goal 11000
python3 money_plan.py car --price 55000 --down 11000 --apr 10 --months 72 --insurance 600
python3 money_plan.py invest --monthly 500 --years 10 20 30 --rate 7
```

## How the math works

- `progressiveTax` walks a bracket table: each slice of income between two thresholds is taxed at that slice's rate and the slices add up. Federal and Arkansas use the same function with different tables.
- `annualTaxes` applies the standard deduction, adds FICA (7.65% of W-2 wages) and, for 1099 income, self-employment tax on 92.35% of profit with half of it deducted from adjusted gross income. `monthlyTakeHome` divides the annual total by twelve.
- `loanPayment` is the standard amortization formula: payment = P × r / (1 − (1 + r)^−n), with r the monthly rate and n the number of months. A zero rate falls back to P / n.
- `futureValue` compounds monthly: a lump sum grows by (1 + r)^n and a monthly contribution grows to M × ((1 + r)^n − 1) / r.
- `carCosts` adds payment, insurance, fuel (miles ÷ mpg × price), tires (a set spread over its life), maintenance and Arkansas personal property tax (20% assessment × county millage), then divides by each rule's percentage to get the gross income that rule requires: a lender payment-to-income cap of 15 to 20%, the 20/4/10 guideline (all car costs under 10% of gross), and a looser 20%-of-take-home test.
- The page wiring at the bottom of `app.js` reads the inputs, calls the functions and writes HTML tables. It runs only when `document` exists, so Node can import the math for the tests without a browser.

## Ideas for extending it

- Add tax tables for other states and a dropdown to pick one.
- Save inputs in `localStorage` so they survive a refresh.
- Chart the savings timeline with inline SVG.

## License

MIT. See `LICENSE`.
