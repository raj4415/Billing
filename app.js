// Invoice form: add/remove rows, product picker, live totals.
// The server recalculates everything on save; this is only a preview.
(function () {
  var form = document.getElementById('invoice-form');
  if (!form) return;

  var products = JSON.parse(document.getElementById('products-data').textContent);
  var cur = document.body.getAttribute('data-currency') || '';
  var tbody = form.querySelector('#items tbody');
  var tpl = document.getElementById('row-tpl');

  function num(v) {
    var n = parseFloat(String(v).replace(/,/g, ''));
    return isNaN(n) ? 0 : n;
  }
  function fmt(n) {
    return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function q(row, name) { return row.querySelector('[name=' + name + ']'); }

  function fillPicker(row) {
    var sel = row.querySelector('.pick');
    products.forEach(function (p, i) {
      var o = document.createElement('option');
      o.value = i;
      o.textContent = p.name;
      sel.appendChild(o);
    });
  }

  function addRow() {
    var row = tpl.content.firstElementChild.cloneNode(true);
    fillPicker(row);
    tbody.appendChild(row);
    return row;
  }

  function recalc() {
    var sub = 0, lines = [];
    tbody.querySelectorAll('tr.item').forEach(function (r) {
      var line = Math.round(num(q(r, 'item_qty').value) * num(q(r, 'item_price').value) * 100) / 100;
      sub += line;
      lines.push([line, num(q(r, 'item_tax').value)]);
      r.querySelector('.amt').textContent = fmt(line);
    });
    var type = form.querySelector('[name=discount_type]').value;
    var dv = num(form.querySelector('[name=discount_value]').value);
    var disc = 0;
    if (type === 'percent') disc = sub * Math.min(dv, 100) / 100;
    else if (type === 'fixed') disc = Math.min(dv, sub);
    disc = Math.round(disc * 100) / 100;
    var factor = sub ? (sub - disc) / sub : 0, groups = {}, tax = 0;
    lines.forEach(function (l) { groups[l[1]] = (groups[l[1]] || 0) + l[0] * factor; });
    Object.keys(groups).forEach(function (rate) { tax += Math.round(groups[rate] * num(rate)) / 100; });
    document.getElementById('t-sub').textContent = cur + fmt(sub);
    document.getElementById('t-disc').textContent = '-' + cur + fmt(disc);
    document.getElementById('t-tax').textContent = cur + fmt(tax);
    document.getElementById('t-total').textContent = cur + fmt(sub - disc + tax);
  }

  tbody.querySelectorAll('tr.item').forEach(fillPicker);

  tbody.addEventListener('change', function (e) {
    if (!e.target.classList.contains('pick') || e.target.value === '') return;
    var row = e.target.closest('tr'), p = products[+e.target.value];
    q(row, 'item_desc').value = p.name;
    q(row, 'item_price').value = p.price;
    q(row, 'item_tax').value = p.tax;
    recalc();
  });
  tbody.addEventListener('click', function (e) {
    if (!e.target.classList.contains('rm')) return;
    if (tbody.querySelectorAll('tr.item').length > 1) e.target.closest('tr').remove();
    recalc();
  });
  form.addEventListener('input', recalc);
  form.querySelector('[name=discount_type]').addEventListener('change', recalc);
  document.getElementById('add-row').addEventListener('click', function () {
    addRow().querySelector('.pick').focus();
  });
  recalc();
})();
