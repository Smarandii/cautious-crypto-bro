let selectedBalanceDate;
let lastBalanceFeed;
let balanceRefreshPending = false;
let balanceChartObserver;

function renderDailyBalance(feed, refreshFailed = false) {
  balanceChartObserver?.disconnect();
  let section = document.querySelector("#daily-balance");
  if (!section) {
    section = document.createElement("section");
    section.id = "daily-balance";
    section.className = "balance-panel";
    document.querySelector("#trade-history, #scorecard").before(section);
  }
  const focusedDate = section.contains(document.activeElement)
    ? document.activeElement.dataset.date
    : null;
  const focusedSelect =
    section.contains(document.activeElement) &&
    document.activeElement.tagName === "SELECT";
  const priorScroll = section.querySelector(
    ".balance-chart-scroll",
  )?.scrollLeft;
  const priorBenchmarkMethod = section.querySelector(".benchmark-method")?.open;
  section.replaceChildren();
  const add = (tag, text, parent = section) => {
    const element = document.createElement(tag);
    element.textContent = text;
    parent.append(element);
    return element;
  };
  const number = (value, digits = 2) =>
    Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
  const signed = (value) =>
    value == null
      ? "Unavailable"
      : `${Number(value) > 0 ? "+" : ""}${number(value, 8)}`;
  add("p", "BYBIT DEMO · ACCOUNT", section).className = "balance-eyebrow";
  add("h2", "Daily wallet balance");
  add(
    "p",
    "USDT · UTC days · Open P&L excluded. Select a day to see its trades and account activity.",
  ).className = "balance-note";
  const history = feed?.history;
  const captured = Date.parse(history?.as_of);
  const age = Date.now() - captured;
  const stale =
    !Number.isFinite(captured) || age > 30 * 60 * 1000 || age < -5 * 60 * 1000;
  const status = add("p", "");
  status.className = "balance-note";
  status.setAttribute("role", "status");
  status.textContent = `${refreshFailed || feed?.refresh_failed ? "Refresh failed; last verified data retained. " : ""}${stale ? "STALE or unavailable balance evidence. " : ""}${Number.isFinite(captured) ? `Updated ${new Date(captured).toISOString().replace("T", " ")}. ` : ""}Published every 15 minutes.`;
  if (!history || !Array.isArray(history.days) || !history.days.length) {
    add("p", "Daily balance unavailable. No historical values inferred.");
    const point = feed?.benchmark?.history?.days?.at(-1);
    if (point)
      add(
        "p",
        `BTC hold ${number(point.value)} USDT · ${point.date} · ${feed.benchmark.history.as_of}${feed.benchmark.refresh_failed ? " · retained after refresh failure" : ""}`,
      ).className = "balance-btc-value";
    return;
  }
  const days = history.days;
  const benchmark = feed?.benchmark?.history;
  const btcDays = new Map(
    (benchmark?.days ?? []).map((day) => [day.date, day]),
  );
  const btcAsOf = Date.parse(benchmark?.as_of);
  const btcStale =
    !Number.isFinite(btcAsOf) ||
    Date.now() - btcAsOf > 30 * 60 * 1000 ||
    btcAsOf > Date.now() + 5 * 60 * 1000;
  const legend = add("p", "");
  legend.className = "balance-legend";
  add("span", "▮ Wallet balance · ", legend);
  add("span", "━ BTC buy & hold (marked value)", legend).className =
    "balance-btc-value";
  add(
    "p",
    benchmark
      ? `BTC hold from 2026-10-04 · ${feed.benchmark.refresh_failed ? "Refresh failed; previous benchmark retained. " : ""}${btcStale ? "STALE. " : ""}Updated ${new Date(btcAsOf).toISOString().slice(0, 16).replace("T", " ")} UTC.`
      : "BTC benchmark unavailable: verified starting capital and complete spot prices are required.",
  ).className = "balance-note";
  const method = add("details", "");
  method.className = "benchmark-method";
  method.open = !!priorBenchmarkMethod;
  add("summary", "How this comparison works", method);
  add(
    "p",
    benchmark
      ? `Hypothetical BTC/USDT spot purchase at 2026-10-04 00:00 UTC: ${number(benchmark.capital)} USDT at ${number(benchmark.entry_price)} USDT/BTC. Fixed holding, gross of trading costs; no later benchmark deposits or withdrawals.`
      : "The comparison requires independently verified opening capital and complete BTC/USDT spot prices. Missing evidence stays unavailable.",
    method,
  );
  add(
    "p",
    "Wallet bars include actual fees, funding and account cash movements, and exclude open P&L. BTC is marked to market. The unadjusted gap is not strategy alpha; inspect selected-day account activity for cash movements.",
    method,
  );
  add(
    "p",
    `${days[0].date} – ${days.at(-1).date} · Last day is provisional; gaps remain unknown. No account activity is omitted within this window.`,
  ).className = "balance-note";
  if (!days.some((day) => day.date === selectedBalanceDate)) {
    selectedBalanceDate =
      days.findLast((day) => day.complete)?.date ?? days.at(-1).date;
  }
  const summary = add("div", "");
  summary.className = "balance-summary";
  summary.setAttribute("aria-live", "polite");
  const dayLabel = add("label", "Choose day ");
  const select = add("select", "", dayLabel);
  select.setAttribute("aria-label", "Balance day");
  for (const day of days) {
    const option = add(
      "option",
      `${day.date}${day.complete ? "" : " · provisional / missing"}`,
      select,
    );
    option.value = day.date;
  }
  const chart = add("div", "");
  chart.className = "balance-chart";
  const values = days.flatMap((day) => [
    Number(day.balance) || 0,
    Number(btcDays.get(day.date)?.value) || 0,
  ]);
  const negative = values.some((value) => value < 0);
  const max = Math.max(1, ...values.map(Math.abs));
  const zero = negative ? 50 : 0;
  const axis = add("div", "", chart);
  axis.className = "balance-axis";
  for (const value of [max, negative ? 0 : max / 2, negative ? -max : 0])
    add("span", number(value, 0), axis);
  const scroll = add("div", "", chart);
  scroll.className = "balance-chart-scroll";
  const bars = add("div", "", scroll);
  bars.className = "balance-bars";
  bars.setAttribute("role", "group");
  bars.setAttribute(
    "aria-label",
    "Daily USDT wallet balance; arrow keys change day",
  );
  const buttons = [];
  const overlay = benchmark
    ? document.createElementNS("http://www.w3.org/2000/svg", "svg")
    : null;
  if (overlay) {
    overlay.classList.add("balance-btc-overlay");
    overlay.setAttribute("aria-hidden", "true");
    bars.append(overlay);
  }
  function drawBenchmark() {
    if (!overlay || !buttons.length) return;
    const bounds = bars.getBoundingClientRect();
    const height = buttons[0].getBoundingClientRect().height;
    overlay.setAttribute("viewBox", `0 0 ${bounds.width} ${height}`);
    overlay.replaceChildren();
    let previous;
    for (const button of buttons) {
      const day = btcDays.get(button.dataset.date);
      if (!day) {
        previous = null;
        continue;
      }
      const rect = button.getBoundingClientRect();
      const point = {
        x: rect.left - bounds.left + rect.width / 2,
        y:
          height *
          (1 -
            (negative ? 0.5 : 0) -
            Number(day.value) / (negative ? 2 * max : max)),
        complete: day.complete,
      };
      if (previous) {
        const line = document.createElementNS(overlay.namespaceURI, "line");
        for (const [key, value] of Object.entries({
          x1: previous.x,
          y1: previous.y,
          x2: point.x,
          y2: point.y,
        }))
          line.setAttribute(key, String(value));
        if (!point.complete || !previous.complete)
          line.setAttribute("stroke-dasharray", "4 3");
        overlay.append(line);
      }
      const circle = document.createElementNS(overlay.namespaceURI, "circle");
      circle.setAttribute("cx", String(point.x));
      circle.setAttribute("cy", String(point.y));
      circle.setAttribute(
        "r",
        button.dataset.date === selectedBalanceDate ? "4" : "2",
      );
      overlay.append(circle);
      previous = point;
    }
  }
  const detail = add("div", "");
  detail.className = "balance-detail";
  detail.id = "balance-day-detail";

  function showDay(date, focus = false) {
    selectedBalanceDate = date;
    select.value = date;
    const day = days.find((item) => item.date === date);
    summary.replaceChildren();
    add(
      "p",
      `${date} · ${day.complete ? "Closing wallet balance" : "Provisional / incomplete day"}`,
      summary,
    );
    add(
      "strong",
      day.balance == null ? "Unavailable" : `${number(day.balance)} USDT`,
      summary,
    );
    const delta = add(
      "p",
      `Day change ${signed(day.change)}${day.change == null ? "" : " USDT"}`,
      summary,
    );
    delta.className =
      day.change == null
        ? ""
        : Number(day.change) >= 0
          ? "balance-positive"
          : "balance-negative";
    const btcDay = btcDays.get(date);
    add(
      "p",
      btcDay
        ? `BTC hold ${number(btcDay.value)} USDT · ${btcDay.complete ? "Daily close" : "Provisional last price"} ${number(btcDay.price)} USDT/BTC · Return since purchase ${signed((Number(btcDay.price) / Number(benchmark.entry_price) - 1) * 100)}%${btcStale || feed.benchmark.refresh_failed ? " · stale / retained" : ""}`
        : "BTC hold unavailable for this day.",
      summary,
    ).className = "balance-btc-value";
    if (btcDay && day.balance != null)
      add(
        "p",
        `Unadjusted balance gap ${signed(Number(day.balance) - Number(btcDay.value))} USDT`,
        summary,
      ).className = "balance-note";
    buttons.forEach((button) => {
      const selected = button.dataset.date === date;
      button.setAttribute("aria-pressed", String(selected));
      button.tabIndex = selected ? 0 : -1;
      if (selected && focus) button.focus();
      if (selected) {
        const bounds = button.getBoundingClientRect();
        const viewport = scroll.getBoundingClientRect();
        if (bounds.left < viewport.left || bounds.right > viewport.right) {
          scroll.scrollLeft +=
            bounds.left -
            viewport.left -
            scroll.clientWidth / 2 +
            bounds.width / 2;
        }
      }
    });
    drawBenchmark();
    detail.replaceChildren();
    add("h3", `Trades and account activity · ${date}`, detail);
    if (day.balance == null) {
      add(
        "p",
        "Coverage unavailable for this day; an empty list does not mean zero trades.",
        detail,
      );
      return;
    }
    add(
      "p",
      `${day.activity.length} account events · Fills, fees, funding and transfers. Buy/Sell is execution direction. Wallet changes include non-strategy activity.`,
      detail,
    ).className = "balance-note";
    if (!day.activity.length) {
      add(
        "p",
        day.complete
          ? "No account activity on this day."
          : "No account activity in the captured part of this day.",
        detail,
      );
      return;
    }
    const region = add("div", "", detail);
    region.className = "trade-table-scroll";
    region.tabIndex = 0;
    region.setAttribute("role", "region");
    region.setAttribute(
      "aria-label",
      `${date} account activity; scroll for all columns`,
    );
    const table = add("table", "", region);
    add(
      "caption",
      `All ${day.activity.length} account events · ${date} UTC · USDT amounts`,
      table,
    );
    const heading = add("tr", "", add("thead", "", table));
    for (const label of [
      "Time · UTC",
      "Activity",
      "Symbol",
      "Buy / Sell",
      "Quantity",
      "Price",
      "Cash flow",
      "Fee",
      "Funding",
      "Wallet change",
    ])
      add("th", label, heading).scope = "col";
    const body = add("tbody", "", table);
    for (const activity of day.activity) {
      const row = add("tr", "", body);
      for (const value of [
        new Date(activity.at).toISOString().slice(11, 23),
        activity.kind.replaceAll("_", " "),
        activity.symbol || "—",
        ["Buy", "Sell"].includes(activity.side) ? activity.side : "—",
        number(activity.quantity, 8),
        number(activity.price, 8),
        signed(activity.cash_flow),
        number(activity.fee, 8),
        signed(activity.funding),
        signed(activity.wallet_change),
      ])
        add("td", value, row);
    }
  }

  days.forEach((day, index) => {
    const column = add("div", "", bars);
    column.className = "balance-column";
    const button = add("button", "", column);
    button.type = "button";
    button.dataset.date = day.date;
    button.className = `balance-bar ${day.complete ? "" : "balance-provisional"}`;
    button.setAttribute("aria-controls", detail.id);
    button.setAttribute(
      "aria-label",
      `${day.date}: ${day.balance == null ? "balance unavailable" : `${number(day.balance)} USDT`}; change ${signed(day.change)}${day.complete ? "" : "; provisional or missing"}`,
    );
    const fill = add("span", "", button);
    fill.className = "balance-bar-fill";
    const value = day.balance == null ? 0 : Number(day.balance);
    const height = (Math.abs(value) / (negative ? 2 * max : max)) * 100;
    fill.style.height = `${height}%`;
    fill.style.bottom = `${value < 0 ? zero - height : zero}%`;
    add("span", day.date.slice(8), column).className = "balance-date";
    button.addEventListener("click", () => showDay(day.date));
    button.addEventListener("keydown", (event) => {
      const target = {
        ArrowLeft: index - 1,
        ArrowRight: index + 1,
        Home: 0,
        End: days.length - 1,
      }[event.key];
      if (target == null) return;
      event.preventDefault();
      showDay(days[Math.max(0, Math.min(days.length - 1, target))].date, true);
    });
    buttons.push(button);
  });
  for (const button of buttons) {
    const btc = btcDays.get(button.dataset.date);
    button.setAttribute(
      "aria-label",
      `${button.getAttribute("aria-label")}; BTC hold ${btc ? `${number(btc.value)} USDT${btc.complete ? "" : ", provisional"}` : "unavailable"}`,
    );
  }
  if (overlay) {
    balanceChartObserver = new ResizeObserver(drawBenchmark);
    balanceChartObserver.observe(bars);
  }
  select.addEventListener("change", () => showDay(select.value));
  showDay(selectedBalanceDate);
  if (priorScroll != null) scroll.scrollLeft = priorScroll;
  if (focusedDate)
    buttons.find((button) => button.dataset.date === focusedDate)?.focus();
  if (focusedSelect) select.focus();
}

async function refreshDailyBalance() {
  if (balanceRefreshPending) return;
  balanceRefreshPending = true;
  try {
    const response = await fetch("balance.json", {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("balance unavailable");
    const feed = await response.json();
    if (![1, 2].includes(feed.schema_version))
      throw new Error("unsupported balance feed");
    renderDailyBalance(feed);
    lastBalanceFeed = feed;
  } catch (error) {
    renderDailyBalance(lastBalanceFeed, true);
  } finally {
    balanceRefreshPending = false;
  }
}

refreshDailyBalance();
setInterval(refreshDailyBalance, 60000);
