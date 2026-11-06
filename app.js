// Money plan calculator: the math behind the budget, car and investing numbers.
//
// Everything in this file is a plain function with no browser dependencies, so
// the same code runs in the page (index.html) and in the test (test.mjs).
// Tax figures are 2026 single-filer estimates (standard deduction, no credits)
// and match money_plan.py in this repository.

export const FED_STD_DEDUCTION = 16100;
export const FED_BRACKETS = [
  [12400, 0.10],
  [50400, 0.12],
  [105700, 0.22],
  [201775, 0.24],
  [256225, 0.32],
  [640600, 0.35],
  [Infinity, 0.37],
];

// Arkansas 2026 (DFA withholding formula effective 1 Jan 2026).
export const AR_STD_DEDUCTION = 2470;
export const AR_PERSONAL_CREDIT = 29;
export const AR_BRACKETS = [
  [5599, 0.0],
  [11199, 0.02],
  [15999, 0.03],
  [26399, 0.034],
  [Infinity, 0.037],
];

export const FICA_RATE = 0.0765;      // employee share of Social Security + Medicare
export const SE_TAX_RATE = 0.153;     // self-employment tax on 92.35% of 1099 profit

// Tax owed on `taxable` dollars under a progressive bracket table.
export function progressiveTax(taxable, brackets) {
  let tax = 0;
  let lower = 0;
  for (const [upper, rate] of brackets) {
    if (taxable <= lower) break;
    tax += (Math.min(taxable, upper) - lower) * rate;
    lower = upper;
  }
  return tax;
}

// Annual federal, Arkansas and FICA taxes for W-2 wages plus optional 1099 profit.
export function annualTaxes(grossW2, net1099 = 0) {
  const seTax = SE_TAX_RATE * net1099 * 0.9235;
  const agi = grossW2 + net1099 - seTax / 2;
  const federal = progressiveTax(Math.max(0, agi - FED_STD_DEDUCTION), FED_BRACKETS) + seTax;
  const arkansas = Math.max(
    0,
    progressiveTax(Math.max(0, agi - AR_STD_DEDUCTION), AR_BRACKETS) - AR_PERSONAL_CREDIT,
  );
  const fica = FICA_RATE * grossW2;
  return { federal, arkansas, fica, total: federal + arkansas + fica };
}

// What lands in the bank each month from a monthly gross figure.
export function monthlyTakeHome(grossMonthly, net1099Monthly = 0) {
  const t = annualTaxes(grossMonthly * 12, net1099Monthly * 12);
  return grossMonthly + net1099Monthly - t.total / 12;
}

// Standard amortized loan payment: principal, APR in percent, term in months.
export function loanPayment(principal, aprPct, months) {
  if (principal <= 0) return 0;
  const r = aprPct / 100 / 12;
  if (r === 0) return principal / months;
  return (principal * r) / (1 - Math.pow(1 + r, -months));
}

// Balance after investing `monthly` for `years` at `ratePct` a year, compounded monthly.
export function futureValue(monthly, years, ratePct, start = 0) {
  const r = ratePct / 100 / 12;
  const n = Math.round(years * 12);
  const fromStart = start * Math.pow(1 + r, n);
  const fromMonthly = r === 0 ? monthly * n : monthly * ((Math.pow(1 + r, n) - 1) / r);
  return fromStart + fromMonthly;
}

// Monthly budget: take-home, bills, what is left, and the car share of gross.
export function budget({ income, side = 0, rent, utilities, carPayment, insurance, food, gas, phone, misc }) {
  const takeHome = monthlyTakeHome(income, side);
  const fixed = rent + utilities + carPayment + insurance;
  const living = food + gas + phone + misc;
  const left = takeHome - fixed - living;
  const carShare = (carPayment + insurance) / Math.max(income + side, 1);
  return { takeHome, fixed, living, left, carShare };
}

// Months needed to save `goal` at a given monthly surplus (Infinity if nothing is saved).
export function monthsToGoal(goal, savedPerMonth) {
  return savedPerMonth > 0 ? goal / savedPerMonth : Infinity;
}

// All-in monthly cost of a car and the gross income common rules would require.
export function carCosts({
  price, down = 0, aprPct, months = 72, insurance, miles = 10000, mpg = 18,
  fuelPrice = 4.62, tireSetCost = 2000, tireLifeMiles = 12000, maintenancePerYear = 1600,
  salesTaxPct = 9.0, millage = 55,
}) {
  const salesTax = price * salesTaxPct / 100;
  const financed = Math.max(0, price - down);
  const payment = loanPayment(financed, aprPct, months);
  const totalInterest = payment * months - financed;
  const fuel = (miles / mpg) * fuelPrice / 12;
  const tires = tireSetCost * (miles / tireLifeMiles) / 12;
  const maintenance = maintenancePerYear / 12;
  const propertyTax = price * 0.20 * millage / 1000 / 12;
  const allIn = payment + insurance + fuel + tires + maintenance + propertyTax;
  return {
    salesTax, financed, payment, totalInterest, fuel, tires, maintenance, propertyTax, allIn,
    incomeNeeded: {
      pti20: (payment + insurance) / 0.20,   // lender payment-to-income cap of 20%
      pti15: (payment + insurance) / 0.15,   // stricter lender cap of 15%
      rule10: allIn / 0.10,                  // 20/4/10: all car costs under 10% of gross
      takeHome20: allIn / 0.20 / 0.80,       // all car costs under 20% of take-home (~80% of gross)
    },
  };
}

export const money = (x) =>
  (x === Infinity ? 'never' : '$' + Math.round(x).toLocaleString('en-US'));

// ---------------------------------------------------------------------------
// Page wiring. Runs only in a browser; the test imports the functions above.
// ---------------------------------------------------------------------------
if (typeof document !== 'undefined') {
  const num = (id) => parseFloat(document.getElementById(id).value) || 0;
  const out = (id, html) => { document.getElementById(id).innerHTML = html; };
  const row = (label, value, note = '') =>
    `<tr><td>${label}</td><td class="num">${value}</td><td class="note">${note}</td></tr>`;

  function renderBudget() {
    const b = budget({
      income: num('income'), side: num('side'), rent: num('rent'), utilities: num('utilities'),
      carPayment: num('carPayment'), insurance: num('insurance'), food: num('food'),
      gas: num('gas'), phone: num('phone'), misc: num('misc'),
    });
    const warn = b.left < 0 ? 'This budget does not balance; nothing can be saved until income rises or a bill falls.' : '';
    out('budgetOut', `<table>
      ${row('Estimated take-home', money(b.takeHome), 'after federal, Arkansas and FICA taxes')}
      ${row('Fixed bills', money(b.fixed), 'rent, utilities, car payment, insurance')}
      ${row('Living costs', money(b.living), 'food, gas, phone, misc')}
      ${row('Left to save', money(b.left), warn)}
      ${row('Car share of gross', (b.carShare * 100).toFixed(0) + '%', 'lenders usually cap payment plus insurance near 15 to 20%')}
    </table>`);

    const goal = num('goal');
    const rows = [2000, 3000, 4000, 5000].map((inc) => {
      const th = monthlyTakeHome(inc);
      const saved = th - b.fixed - b.living;
      const m = monthsToGoal(goal, saved);
      return row(money(inc) + ' gross', money(saved) + ' saved', m === Infinity ? 'never' : m.toFixed(1) + ' months to ' + money(goal));
    }).join('');
    out('timelineOut', `<table>${rows}</table>`);
  }

  function renderCar() {
    const c = carCosts({
      price: num('price'), down: num('down'), aprPct: num('apr'), months: num('months'),
      insurance: num('carInsurance'), miles: num('miles'), mpg: num('mpg'), fuelPrice: num('fuelPrice'),
    });
    out('carOut', `<table>
      ${row('Sales tax due at registration', money(c.salesTax), '6.5% state plus local, paid in cash')}
      ${row('Loan payment', money(c.payment), money(c.financed) + ' financed; ' + money(c.totalInterest) + ' total interest')}
      ${row('Insurance', money(num('carInsurance')), '')}
      ${row('Fuel', money(c.fuel), '')}
      ${row('Tires', money(c.tires), '')}
      ${row('Maintenance and repairs', money(c.maintenance), '')}
      ${row('Arkansas personal property tax', money(c.propertyTax), '20% assessment times county millage')}
      ${row('All-in per month', money(c.allIn), '')}
      ${row('Gross income needed, lender 20% cap', money(c.incomeNeeded.pti20) + ' / month', 'payment plus insurance')}
      ${row('Gross income needed, lender 15% cap', money(c.incomeNeeded.pti15) + ' / month', '')}
      ${row('Gross income needed, 20/4/10 rule', money(c.incomeNeeded.rule10) + ' / month', 'all car costs under 10% of gross')}
      ${row('Gross income needed, 20% of take-home', money(c.incomeNeeded.takeHome20) + ' / month', 'looser test')}
    </table>`);
  }

  function renderInvest() {
    const monthly = num('monthly');
    const rate = num('rate');
    const rows = [5, 10, 20, 30, 40].map((y) => {
      const fv = futureValue(monthly, y, rate);
      const put = monthly * y * 12;
      return row(y + ' years', money(fv), money(put) + ' contributed, ' + money(fv - put) + ' growth');
    }).join('');
    out('investOut', `<table>${rows}</table>
      <p class="note">7% a year is a common long-run estimate for a broad stock index after inflation. Future returns can be lower; try 5% too.</p>`);
  }

  const renderAll = () => { renderBudget(); renderCar(); renderInvest(); };
  document.querySelectorAll('input').forEach((el) => el.addEventListener('input', renderAll));
  renderAll();
}
