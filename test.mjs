// Run with: node test.mjs
// Checks the calculator's math against the Python version and hand-worked values.
import assert from 'node:assert/strict';
import {
  annualTaxes, monthlyTakeHome, loanPayment, futureValue, budget, carCosts, monthsToGoal,
} from './app.js';

const close = (actual, expected, tolerance, label) => {
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label}: got ${actual}, expected about ${expected}`);
};

// Take-home on $2,000 a month gross matches money_plan.py ($1,747).
close(monthlyTakeHome(2000), 1747, 2, 'take-home at $2,000');
close(monthlyTakeHome(3000), 2522, 2, 'take-home at $3,000');
close(monthlyTakeHome(5000), 4055, 2, 'take-home at $5,000');

// Arkansas tax on $24,000: DFA 2026 formula gives about $415.
close(annualTaxes(24000).arkansas, 415, 3, 'Arkansas tax on $24k');

// $44,000 at 10% over 72 months is about $815 a month.
close(loanPayment(44000, 10, 72), 815, 1, 'loan payment');
assert.equal(loanPayment(0, 10, 72), 0, 'no loan, no payment');
close(loanPayment(12000, 0, 12), 1000, 0.01, 'zero-interest loan');

// $1,900 a month at 7% for 10 years grows to about $328,861.
close(futureValue(1900, 10, 7), 328861, 50, 'future value');
close(futureValue(0, 10, 7, 1000), 1000 * Math.pow(1 + 0.07 / 12, 120), 0.01, 'lump sum only');

// A sample budget: $2,500 gross, $1,450 of fixed bills and $605 of living costs leaves about $84.
const b = budget({ income: 2500, rent: 650, utilities: 150, carPayment: 350, insurance: 300, food: 320, gas: 110, phone: 25, misc: 150 });
close(b.left, 84, 3, 'budget left over');
close(b.carShare, 0.26, 0.001, 'car share of gross');
assert.equal(monthsToGoal(11000, -100), Infinity, 'no savings means never');
close(monthsToGoal(11000, 1000), 11, 0.001, 'months to goal');

// $55k Grand Sport scenario: all-in near $1,950 and about $7,076 gross needed at a 20% cap.
const c = carCosts({ price: 55000, down: 11000, aprPct: 10, months: 72, insurance: 600 });
close(c.allIn, 1952, 5, 'car all-in');
close(c.incomeNeeded.pti20, 7076, 10, 'income needed at 20% PTI');

console.log('All calculator checks passed.');
