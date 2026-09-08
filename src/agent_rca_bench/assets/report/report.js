/* Agent RCA Bench report renderer.
 *
 * Reads three inlined payloads: the authoritative report JSON, the view model
 * derived from it, and the static interface strings. Every sentence whose
 * wording depends on a measured value already exists in the view model. This
 * file lays those values out and draws them; it never decides what they mean.
 */
(() => {
  "use strict";

  const payload = (id) => JSON.parse(document.getElementById(id).textContent);
  const data = payload("semantic-rca-report");
  const view = payload("semantic-rca-view");
  const strings = payload("semantic-rca-i18n");

  const LANGUAGES = view.languages;
  const SECTIONS = [
    "overview",
    "interface",
    "semantic",
    "retrieval",
    "models",
    "cases",
    "resources",
    "questions",
    "method",
  ];

  let language = "en";

  // --- primitives ---------------------------------------------------------

  const t = (key, params) => {
    const table = strings[language] || strings.en;
    let text = key in table ? table[key] : strings.en[key];
    if (text === undefined) return key;
    if (params) {
      for (const [name, value] of Object.entries(params)) {
        text = text.split(`{${name}}`).join(String(value));
      }
    }
    return text;
  };

  const h = (tag, attrs, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  const svgEl = (tag, attrs) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;
      if (key === "text") node.textContent = String(value);
      else node.setAttribute(key, String(value));
    }
    return node;
  };

  const isNum = (value) => typeof value === "number" && Number.isFinite(value);
  const NA = () => t("label.na");

  const num = (value, digits) => {
    if (!isNum(value)) return NA();
    const abs = Math.abs(value);
    const decimals = digits !== undefined ? digits : abs < 10 && !Number.isInteger(value) ? 2 : 0;
    return value.toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  };

  const signed = (value, digits) => (isNum(value) && value > 0 ? `+${num(value, digits)}` : num(value, digits));

  /* Axis ticks and medians span six orders of magnitude across strips, so they
   * are abbreviated rather than printed in full. Tables keep the exact value. */
  const compact = (value) => {
    if (!isNum(value)) return NA();
    const abs = Math.abs(value);
    const sign = value < 0 ? "−" : value > 0 ? "+" : "";
    if (abs >= 1e6) return `${sign}${(abs / 1e6).toFixed(abs >= 1e7 ? 0 : 1)}M`;
    if (abs >= 1e3) return `${sign}${(abs / 1e3).toFixed(abs >= 1e4 ? 0 : 1)}k`;
    if (abs >= 10 || Number.isInteger(abs)) return `${sign}${abs}`;
    return `${sign}${abs.toFixed(2)}`;
  };

  const pval = (value) => {
    if (!isNum(value)) return NA();
    if (value >= 0.001) return value.toPrecision(3).replace(/0+$/, "").replace(/\.$/, "");
    return value.toExponential(1);
  };

  const money = (value, currency) => (isNum(value) ? `${currency || ""} ${num(value, 4)}`.trim() : NA());

  const treatment = (key) => view.treatment_labels[key] || key;
  const narrative = () => view.narrative[language] || view.narrative.en;

  // --- layout helpers -----------------------------------------------------

  const section = (id, title, lede, ...body) =>
    h(
      "section",
      { id },
      h(
        "div",
        { class: "wrap" },
        h("div", { class: "section-head" }, h("h2", { text: title }), lede ? h("p", { text: lede }) : null),
        ...body,
      ),
    );

  const panel = (summary, ...body) =>
    h("details", { class: "panel" }, h("summary", { text: summary }), h("div", { class: "panel-body" }, ...body));

  const table = (headers, rows) => {
    const head = h(
      "tr",
      {},
      headers.map((header) =>
        h("th", { class: header.numeric ? "n" : null, text: header.label || header }),
      ),
    );
    const body = rows.map((row) =>
      h(
        "tr",
        {},
        row.map((cell) => {
          const value = cell && typeof cell === "object" && !(cell instanceof Node) ? cell : { value: cell };
          const classes = [value.numeric ? "n" : null, value.class || null].filter(Boolean).join(" ");
          return h("td", { class: classes || null }, value.value instanceof Node ? value.value : String(value.value));
        }),
      ),
    );
    return h(
      "div",
      { class: "table-wrap" },
      h("table", {}, h("thead", {}, head), h("tbody", {}, body)),
    );
  };

  /* A signed cell is tinted by direction only. Not-estimable stays grey so a
   * missing endpoint can never be misread as a neutral result. */
  const deltaCell = (value, digits) => ({
    value: signed(value, digits),
    numeric: true,
    class: !isNum(value) ? "na" : value < 0 ? "neg" : value > 0 ? "pos" : null,
  });

  const definitions = (pairs) =>
    h(
      "dl",
      { class: "defs" },
      pairs.flatMap(([term, value]) => [h("dt", { text: term }), h("dd", {}, value)]),
    );

  // --- hero and verdict board ---------------------------------------------

  /* The first screen answers the question it asks. These are the report's own
   * descriptive headline rows, transposed so one interface reads across. The
   * caveat travels with them: none of these three is a tested endpoint, and a
   * reader who stops at the first screen must not come away thinking it is. */
  const heroScoreboard = () => {
    const chart = view.charts.headline;
    const rows = chart.rows;
    const columnKey = {
      accuracy: "hero.board.accuracy",
      cost: "hero.board.cost",
      input_tokens: "hero.board.input",
    };
    // Each row picks its own best, so the basis row is marked only when all
    // three agree; otherwise no row is the basis and the ×1.00 cells say so.
    const bests = new Set(rows.map((row) => row.best));
    const basis = bests.size === 1 ? [...bests][0] : null;
    const cell = (row, key) => {
      if (row.id === "accuracy") return `${num(row.values[key])} / ${num(row.denominator)}`;
      if (row.bounds_labels) return `${num(row.bounds[key][0], 2)}–${num(row.bounds[key][1], 2)}`;
      const ratio = row.ratios[key];
      return isNum(ratio) ? `×${ratio.toFixed(2)}` : NA();
    };
    return h(
      "figure",
      { class: "hero-board" },
      h("figcaption", { text: t("hero.board.title") }),
      h(
        "table",
        null,
        h(
          "thead",
          null,
          h(
            "tr",
            null,
            h("th", { scope: "col" }),
            rows.map((row) => h("th", { scope: "col", text: t(columnKey[row.id]) + (row.bounds_labels ? ` (${row.currency})` : "") })),
          ),
        ),
        h(
          "tbody",
          null,
          chart.treatments.map((key) =>
            h(
              "tr",
              { "data-basis": basis && key === basis ? "true" : null },
              h(
                "th",
                { scope: "row" },
                h("span", { class: "arm-chip", "data-arm": key, text: treatment(key) }),
              ),
              rows.map((row) => h("td", { class: row.bounds_labels ? "cost-interval" : null, text: cell(row, key) })),
            ),
          ),
        ),
      ),
      h("p", { class: "hero-board-note", text: t("hero.board.note") }),
      rows.filter((row) => row.bounds_labels).map((row) =>
        h("p", { class: "hero-board-note", text: row.estimate_note[language] }),
      ),
      h("h3", { class: "hero-board-sub", text: t("hero.models.title") }),
      heroModelTable(),
      h("p", {
        class: "hero-board-note",
        text: t("hero.models.note", { runs: view.charts.diagnosis_slope.runs_per_treatment }),
      }),
      h("p", { class: "hero-board-note", text: t("hero.board.caveat") }),
    );
  };

  /* Correct diagnoses per model, the measured count rather than the 40/40/5
   * index, which is a post-measurement construction and does not belong on the
   * first screen next to the pre-specified work. */
  const heroModelTable = () => {
    const chart = view.charts.diagnosis_slope;
    return h(
      "table",
      { class: "hero-models" },
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          h("th", { scope: "col" }),
          chart.treatments.map((key) => h("th", { scope: "col", text: treatment(key) })),
        ),
      ),
      h(
        "tbody",
        null,
        chart.ranked_series.map((row) =>
          h(
            "tr",
            null,
            h("th", { scope: "row", text: row.model }),
            chart.treatments.map((key) => h("td", { text: num(row.values[key]) })),
          ),
        ),
      ),
    );
  };

  const heroSection = () => {
    const facts = view.facts;
    const meta = [
      t("hero.meta.runs", { n: num(facts.completed_cells) }),
      t("hero.meta.models", { n: facts.models }),
      t("hero.meta.cases", { n: facts.transfer_cases }),
      t("hero.meta.micro", { n: facts.micro_cases }),
    ];
    if (facts.publication) {
      meta.push(
        t("hero.meta.measurement_updated", { timestamp: facts.publication.measurement_updated_at }),
        t("hero.meta.report_generated", { timestamp: facts.publication.report_generated_at }),
      );
    }
    const repo = "https://github.com/GreptimeTeam/agent-rca-bench";
    return h(
      "header",
      { class: "hero" },
      h(
        "div",
        { class: "wrap hero-grid" },
        h(
          "div",
          { class: "hero-copy" },
          h("p", { class: "eyebrow", text: t("hero.eyebrow") }),
          h("h1", { text: t("hero.title") }),
          h("p", { class: "lede", text: t("hero.lede") }),
          h("div", { class: "hero-meta" }, meta.map((item) => h("span", { text: item }))),
          h(
            "div",
            { class: "hero-actions" },
            h("a", { href: view.report_json_filename, download: true, text: t("hero.download") }),
            h("a", { href: `${repo}#reproduce-the-published-report`, text: t("hero.reproduce") }),
            h("a", { href: repo, text: t("hero.source") }),
          ),
        ),
        heroScoreboard(),
      ),
    );
  };

  /* The first screen has to stand on its own: what the three interfaces are,
   * then what the measurement found, each with the evidence behind it. */
  const overviewSection = () =>
    h(
      "section",
      { id: "overview" },
      h(
        "div",
        { class: "wrap" },
        h("h2", { text: t("overview.setup_title") }),
        h("p", {
          class: "section-lede",
          text: t("overview.setup_lede", {
            total: view.facts.treatment_count * view.facts.repetitions,
            repetitions: view.facts.repetitions,
            interfaces: view.facts.treatment_count,
          }),
        }),
        armCards(),
        interfaceMatrix(),
        h("h2", { class: "findings-title", text: t("headline.title") }),
        h("p", { class: "section-lede", text: t("headline.lede") }),
        headlineBars(),
        h("h2", { class: "findings-title", text: t("overview.findings_title") }),
        h("div", { class: "findings" }, view.takeaways.map(takeawayRow)),
        h(
          "p",
          { class: "note" },
          t("overview.evidence_note", { alpha: view.evidence_rule.alpha }),
        ),
      ),
    );

  /* Split, Raw and Graph are the names every later chart and table refers back
   * to, so the card leads with the name rather than tucking it underneath. */
  /* Ratios against the best arm. A 2.4x gap has to look like 2.4x; the badge on
   * the row says whether the number is a pre-specified endpoint or a total. */
  const headlineBars = () => {
    const chart = view.charts.headline;
    return h(
      "div",
      { class: "headline" },
      chart.rows.map((row) => {
        const best = row.best;
        if (row.bounds_labels) {
          return h("div", { class: "headline-row" },
            h("div", { class: "headline-head" },
              h("h3", { text: t(`headline.${row.label_key}`) }),
              h("span", { class: "caption", text: row.estimate_note[language] }),
            ),
            h("div", { class: "bars" }, chart.treatments.map((key) =>
              h("div", { class: "bar-row cost-range-row" },
                h("span", { class: "bar-name", text: treatment(key) }),
                h("div", { class: "bar-track cost-range" },
                  h("div", { class: "bar-fill", "data-arm": key, style: `width:${row.bounds_fractions[key][0] * 100}%` }),
                  h("div", { class: "bar-fill range-tail", "data-arm": key, style: `left:${row.bounds_fractions[key][0] * 100}%;width:${(row.bounds_fractions[key][1] - row.bounds_fractions[key][0]) * 100}%` }),
                ),
                h("div", { class: "bar-value" },
                  h("strong", { text: row.bounds_labels[key] }),
                  h("span", { class: "bar-ratio", text: row.bounds_ratio_labels[key] || NA() }),
                ),
              ),
            )),
          );
        }
        if (!row.estimable) {
          return h(
            "div",
            { class: "headline-row" },
            h(
              "div",
              { class: "headline-head" },
              h("h3", { text: t(`headline.${row.label_key}`) }),
              h("span", {
                class: "caption",
                text: row.unavailable_text ? row.unavailable_text[language] : t("headline.not_estimable"),
              }),
            ),
          );
        }
        return h(
          "div",
          { class: "headline-row" },
          h(
            "div",
            { class: "headline-head" },
            h("h3", { text: t(`headline.${row.label_key}`) }),
            h("span", { class: "caption", text: row.estimate_note ? row.estimate_note[language] : t(`headline.${row.label_key}.note`) }),
            h("span", {
              class: "badge badge-sm",
              "data-grade": "descriptive",
              text: t("headline.descriptive"),
            }),
          ),
          h(
            "div",
            { class: "bars" },
            chart.treatments.map((key) => {
              const value = row.values[key];
              const ratio = row.ratios[key];
              return h(
                "div",
                { class: "bar-row", "data-best": String(key === best) },
                h("span", { class: "bar-name", text: treatment(key) }),
                h(
                  "div",
                  { class: "bar-track" },
                  h("div", {
                    class: "bar-fill",
                    "data-arm": key,
                    style: `width:${Math.max(1, (row.fractions[key] || 0) * 100)}%`,
                  }),
                ),
                h(
                  "div",
                  { class: "bar-value" },
                  h("strong", { text: headlineValue(row, value) }),
                  h("span", {
                    class: "bar-ratio",
                    text: ratio ? `×${ratio.toFixed(2)}` : NA(),
                  }),
                ),
              );
            }),
          ),
          row.unit === "currency"
            ? h("p", { class: "caption" }, h("a", { href: `#${language}-resources`, text: t("pricing.title") }))
            : null,
        );
      }),
    );
  };

  const headlineValue = (row, value) => {
    if (row.unit === "runs") {
      return `${num(value)} / ${num(row.denominator)}  ${Math.round((value / row.denominator) * 100)}%`;
    }
    if (row.unit === "currency") return `${row.currency} ${num(value, 2)}`;
    return compact(value).replace(/^\+/, "");
  };

  /* Where the semantic layer earns its keep, and where it does not. */
  const accuracyByLevel = () => {
    const chart = view.charts.accuracy_by_level;
    return h(
      "div",
      { class: "headline" },
      chart.groups.map((group) =>
        h(
          "div",
          { class: "headline-row" },
          h(
            "div",
            { class: "headline-head" },
            h("h3", { text: group.label[language] || group.label.en }),
            h("span", { class: "caption", text: t("label.cases_n", { n: group.cases }) }),
          ),
          h(
            "div",
            { class: "bars" },
            chart.treatments.map((key) => {
              const rate = group.rates[key];
              const counts = group.counts[key];
              const top = Math.max(...Object.values(group.rates));
              return h(
                "div",
                { class: "bar-row", "data-best": String(rate === top) },
                h("span", { class: "bar-name", text: treatment(key) }),
                h(
                  "div",
                  { class: "bar-track" },
                  h("div", {
                    class: "bar-fill",
                    "data-arm": key,
                    style: `width:${Math.max(1, rate * 100)}%`,
                  }),
                ),
                h(
                  "div",
                  { class: "bar-value" },
                  h("strong", { text: `${Math.round(rate * 100)}%` }),
                  h("span", { class: "bar-ratio", text: `${counts.correct}/${counts.runs}` }),
                ),
              );
            }),
          ),
        ),
      ),
    );
  };

  const armCards = () =>
    h(
      "div",
      { class: "arms" },
      view.treatments.map((key) =>
        h(
          "div",
          { class: "arm" },
          h("span", { class: "arm-name", text: treatment(key) }),
          h("h3", { text: t(`arm.${key}.name`) }),
          h("p", { class: "caption", text: t(`arm.${key}.gloss`) }),
        ),
      ),
    );

  /* Built from the protocol's own component list, so the definition of each arm
   * cannot drift away from what the runs were actually given. */
  const interfaceMatrix = () => {
    const matrix = view.interface_matrix;
    return h(
      "details",
      { class: "panel matrix-panel" },
      h("summary", { text: t("panel.matrix") }),
      h(
        "div",
        { class: "panel-body" },
        table(
          [
            t("th.value"),
            ...matrix.treatments.map((key) => ({ label: t(`arm.${key}.name`), numeric: true })),
          ],
          matrix.components.map((component) => [
            t(`component.${component}`),
            ...matrix.treatments.map((key) => ({
              value: matrix.present[key].includes(component) ? "●" : "·",
              numeric: true,
              class: matrix.present[key].includes(component) ? null : "na",
            })),
          ]),
        ),
      ),
    );
  };

  const takeawayRow = (takeaway, index) => {
    const copy = narrative().takeaways[takeaway.id];
    return h(
      "article",
      { class: "finding", "data-grade": takeaway.grade },
      h("span", { class: "finding-index", text: index + 1 }),
      h(
        "div",
        { class: "finding-body" },
        h("h3", { text: copy.headline }),
        h(
          "div",
          { class: "finding-grade" },
          h("span", {
            class: "badge",
            "data-grade": takeaway.grade,
            text: t(`grade.${takeaway.grade}`),
          }),
          h("span", { class: "caption", text: t(`grade.${takeaway.grade}.gloss`) }),
        ),
        h("ul", { class: "finding-support plain" }, copy.support.map((text) => h("li", { text }))),
      ),
    );
  };

  const verdictBoard = () => {
    const rows = view.verdicts.map((verdict) => {
      const endpoints = verdict.endpoints;
      const marks = endpoints
        ? h(
            "div",
            { class: "tally", role: "img", "aria-label": tallyLabel(verdict) },
            Array.from({ length: endpoints.total }, (_, index) =>
              h("i", { "data-on": index < endpoints.significant ? "1" : "0" }),
            ),
          )
        : null;
      return h(
        "div",
        { class: "verdict" },
        h(
          "div",
          {},
          h("p", { class: "eyebrow", text: t(verdict.endpoints ? "role.confirmatory" : "role.descriptive") }),
          verdict.comparison ? h("p", { class: "caption", text: verdict.comparison }) : null,
        ),
        h(
          "div",
          {},
          h("p", { class: "verdict-question", text: t(`question.${verdict.goal}`) }),
          verdict.goal in narrative().family_summaries
            ? h("ul", { class: "verdict-detail plain" }, narrative().family_summaries[verdict.goal].map((text) => h("li", { text })))
            : null,
        ),
        h(
          "div",
          { class: "verdict-status" },
          h("span", { class: "badge", "data-status": verdict.status, text: t(`status.${verdict.status}`) }),
          marks,
          h("span", { class: "tally-label", text: tallyLabel(verdict) }),
        ),
      );
    });
    return h("div", { class: "board" }, rows);
  };

  /* The pre-specified questions and how each family's endpoints landed. This is the
   * formal statement behind the findings above, kept separate so the findings
   * can be read without it. */
  const questionsSection = () =>
    section(
      "questions",
      t("board.title"),
      t("board.note"),
      verdictBoard(),
      panel(t("panel.endpoints"), familyTable("storage_shape"), familyTable("semantic_layer")),
    );

  // Both sentences are decided in the view model; the renderer only prints them.
  const tallyLabel = (verdict) => verdict.tally_text[language] || verdict.tally_text.en;

  // --- signature: paired delta strips -------------------------------------

  const strips = (family) => {
    const items = view.charts.delta_strips.filter((strip) => strip.family === family);
    const byMetric = new Map();
    for (const strip of items) {
      if (!byMetric.has(strip.metric)) byMetric.set(strip.metric, []);
      byMetric.get(strip.metric).push(strip);
    }
    if (!items.length) return [];
    const blocks = [h("p", { class: "strip-lede", text: t("strip.lede") })];
    for (const group of byMetric.values()) {
      const first = group[0];
      blocks.push(
        h(
          "div",
          { class: "family-head" },
          h("h3", { text: t(`metric.${first.metric}`) }),
          h("span", { class: "muted", text: t(`metric.${first.metric}.note`) }),
        ),
        // The axis ends say which side means what, so a dot's position reads
        // without a legend lookup.
        h(
          "div",
          { class: "strip-scale" },
          h("span", {
            class: "scale-left",
            text: t("strip.axis_left", { treatment: first.favors.negative }),
          }),
          h("span", {
            class: "scale-right",
            text: t("strip.axis_right", { treatment: first.favors.positive }),
          }),
        ),
        h("div", { class: "strips" }, group.map(stripRow)),
      );
    }
    return blocks;
  };

  const stripRow = (strip) => {
    const toPercent = (position) => `${((position + 1) / 2) * 100}%`;
    const axis = h("div", { class: "strip-axis" });
    for (const tick of strip.axis.ticks) {
      const mark = h("div", { class: "strip-tick", style: `left:${toPercent(tick.position)}` });
      if (tick.value !== 0) mark.append(h("span", { text: compact(tick.value) }));
      axis.append(mark);
    }
    axis.append(h("div", { class: "strip-zero", style: `left:${toPercent(0)}` }));
    strip.points.forEach((point, index) => {
      axis.append(
        h("div", {
          class: "strip-point",
          "data-side": point.value < 0 ? "negative" : point.value > 0 ? "positive" : "zero",
          style: `left:${toPercent(point.position)}; animation-delay:${index * 22}ms`,
          title: `${point.case_id}: ${signed(point.value, 2)}`,
        }),
      );
    });
    if (strip.median) {
      axis.append(
        h("div", {
          class: "strip-median",
          style: `left:${toPercent(strip.median.position)}`,
          "data-label": compact(strip.median.value),
        }),
      );
    }
    if (!strip.points.length) {
      axis.append(h("span", { class: "caption", style: "position:absolute;left:50%;top:24px;transform:translateX(-50%)", text: t("strip.no_points") }));
    }
    return h(
      "div",
      { class: "strip", "data-grade": strip.significant ? "confirmed" : "none" },
      h(
        "div",
        {},
        h("div", { class: "strip-model", text: strip.model }),
        // A sentence, not a ratio: the reader should not have to decode "6/13".
        h("div", { class: "strip-reading", text: stripReading(strip) }),
      ),
      axis,
      h(
        "div",
        { class: "strip-result" },
        strip.significant
          ? h("span", { class: "badge badge-sm", "data-grade": "confirmed", text: t("grade.confirmed") })
          : null,
        h("span", {
          class: "strip-p",
          text: `${t("strip.median_label")} ${compact(strip.median && strip.median.value)}`,
        }),
        h("span", { class: "strip-p", text: t("strip.holm", { value: pval(strip.holm_adjusted_p) }) }),
      ),
    );
  };

  // --- relative change: how much of the baseline's work each arm spent -----

  /* A zero line down the middle, one bar per model. Left of the line the arm
   * needed less than the baseline on the same incidents; right of it, more. The
   * absolute deltas behind these proportions stay in the strips panel below. */
  const relativeBars = (family) => {
    const group = view.charts.relative_change.find((item) => item.family === family);
    if (!group) return [];
    const blocks = [
      h("p", { class: "strip-lede", text: t("relative.lede") }),
      h("p", { class: "caption", text: group.case_span[language] || group.case_span.en }),
    ];
    for (const metric of group.metrics) {
      blocks.push(
        h(
          "div",
          { class: "family-head" },
          h("h3", { text: metric.label[language] || metric.label.en }),
          h("span", { class: "muted", text: t("relative.note") }),
        ),
        h(
          "div",
          { class: "strip-scale" },
          h("span", {
            class: "scale-left",
            text: t("relative.axis_left", { treatment: group.favors.down }),
          }),
          h("span", {
            class: "scale-right",
            text: t("relative.axis_right", { treatment: group.favors.up }),
          }),
        ),
        h(
          "div",
          { class: "pct-rows" },
          metric.rows.map((row) =>
            h(
              "div",
              { class: "pct-row", "data-direction": row.direction },
              h("span", { class: "pct-name", text: row.model }),
              h(
                "div",
                { class: "pct-track" },
                h("div", { class: "pct-zero" }),
                row.percent === null
                  ? null
                  : h("div", {
                      class: "pct-fill",
                      "data-direction": row.direction,
                      style: `width:${(row.fraction * 50).toFixed(3)}%`,
                    }),
              ),
              h(
                "div",
                { class: "pct-value" },
                h("strong", {
                  text: row.percent === null ? NA() : `${signed(row.percent, 1)}%`,
                }),
                h("span", {
                  class: "pct-cases",
                  text: t("relative.cases", { cases: row.cases }),
                }),
              ),
            ),
          ),
        ),
      );
    }
    return blocks;
  };

  const stripReading = (strip) => strip.reading[language] || strip.reading.en;

  const familyTable = (family) => {
    const items = view.charts.delta_strips.filter((strip) => strip.family === family);
    return table(
      [
        t("th.model"),
        t("th.metric"),
        { label: t("th.eligible_cases"), numeric: true },
        { label: t("th.median_delta"), numeric: true },
        { label: t("th.direction"), numeric: true },
        { label: t("th.unadjusted_p"), numeric: true },
        { label: t("th.holm_p"), numeric: true },
        { label: "m", numeric: true },
      ],
      items.map((strip) => [
        strip.model,
        strip.metric_label[language] || strip.metric_label.en,
        { value: strip.counts.eligible, numeric: true },
        deltaCell(strip.median && strip.median.value, 2),
        { value: `${strip.counts.negative} / ${strip.counts.tied} / ${strip.counts.positive}`, numeric: true },
        { value: pval(strip.unadjusted_p), numeric: true },
        { value: pval(strip.holm_adjusted_p), numeric: true, class: strip.significant ? "neg" : null },
        { value: strip.multiplicity_family_size, numeric: true },
      ]),
    );
  };

  // --- charts -------------------------------------------------------------

  const slopeChart = (chart = view.charts.diagnosis_slope) => {
    const width = 860;
    const height = 430;
    const pad = { top: 24, right: 210, bottom: 34, left: 48 };
    const max = chart.runs_per_treatment;
    const columns = chart.treatments;
    const colors = ["#2563a6", "#a45b17", "#24816a", "#99548e", "#b34448", "#65721c"];
    const x = (index) => pad.left + (index * (width - pad.left - pad.right)) / Math.max(1, columns.length - 1);
    const y = (value) => pad.top + (1 - value / max) * (height - pad.top - pad.bottom);
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": chart.label ? chart.label[language] : t("diagnosis.title") });

    const tickStep = Math.max(1, Math.round(max / 4));
    const ticks = [...new Set([0, ...Array.from({ length: Math.floor(max / tickStep) }, (_, i) => (i + 1) * tickStep), max])];
    for (const value of ticks) {
      svg.append(svgEl("line", { class: "grid", x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value) }));
      svg.append(svgEl("text", { class: "label", x: 22, y: y(value) + 4, "text-anchor": "end", text: num(value) }));
    }
    columns.forEach((column, index) => {
      svg.append(svgEl("text", { class: "label", x: x(index), y: height - 12, "text-anchor": "middle", text: treatment(column) }));
    });
    const endLabels = [];
    const labeled = new Set();
    chart.series.forEach((item, modelIndex) => {
      const color = colors[modelIndex % colors.length];
      const points = columns.map((column, index) => [x(index), y(item.values[column])]);
      const line = svgEl("polyline", { class: "series", style: `stroke:${color}`, points: points.map((point) => point.join(",")).join(" ") });
      line.append(svgEl("title", { text: `${item.model}: ${columns.map((key) => `${treatment(key)} ${item.values[key]}/${max}`).join(", ")}` }));
      svg.append(line);
      points.forEach(([px, py], index) => {
        const value = item.values[columns[index]];
        const dot = svgEl("circle", { class: "series-dot", style: `stroke:${color}`, cx: px, cy: py, r: 4, "data-model": item.model, "data-treatment": columns[index], "data-value": value });
        dot.append(svgEl("title", { text: `${item.model}: ${value}/${max}` }));
        svg.append(dot);
        // Tied models share a coordinate and one value label.
        const key = `${index}:${value}`;
        if (!labeled.has(key)) {
          labeled.add(key);
          svg.append(svgEl("text", { class: "value", x: px + 8, y: py + 4, text: value }));
        }
      });
      const last = points[points.length - 1];
      endLabels.push({ model: item.model, x: last[0], pointY: last[1], y: last[1], color });
    });
    endLabels.sort((a, b) => a.y - b.y);
    for (let index = 1; index < endLabels.length; index += 1) {
      endLabels[index].y = Math.max(endLabels[index].y, endLabels[index - 1].y + 18);
    }
    for (const label of endLabels) {
      svg.append(svgEl("line", { x1: label.x + 26, y1: label.pointY, x2: label.x + 42, y2: label.y, style: `stroke:${label.color};stroke-width:1` }));
      svg.append(svgEl("text", { class: "label", x: label.x + 46, y: label.y + 4, style: `fill:${label.color}`, text: label.model }));
    }
    return h("div", { class: "chart diagnosis-chart" }, svg);
  };

  const reversalChart = (chart) => {
    const groups = chart.groups;
    const treatments = view.treatments;
    const width = 720;
    const barHeight = 18;
    const groupGap = 30;
    const rowHeight = treatments.length * (barHeight + 6);
    const height = groups.length * (rowHeight + groupGap) + 24;
    const pad = { left: 150, right: 60 };
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
    let cursor = 12;
    for (const group of groups) {
      const max = Math.max(...treatments.map((key) => group.runs[key] || 0), 1);
      const groupLabel = group.label[language] || group.label.en;
      svg.append(
        svgEl("text", { class: "value group-label", x: 0, y: cursor + 12, text: groupLabel }),
      );
      svg.append(
        svgEl("text", {
          class: "label",
          x: 0,
          y: cursor + 28,
          text: t("label.cases_n", { n: group.cases }),
        }),
      );
      treatments.forEach((key, index) => {
        const y = cursor + index * (barHeight + 6);
        const value = group.values[key] || 0;
        const full = ((width - pad.left - pad.right) * (group.runs[key] || 0)) / max;
        const filled = ((width - pad.left - pad.right) * value) / max;
        svg.append(svgEl("text", { class: "label", x: pad.left - 8, y: y + 13, "text-anchor": "end", text: treatment(key) }));
        svg.append(svgEl("rect", { class: "bar-alt", x: pad.left, y, width: full, height: barHeight, opacity: 0.32, rx: 2 }));
        svg.append(svgEl("rect", { class: "bar", x: pad.left, y, width: filled, height: barHeight, rx: 2 }));
        svg.append(
          svgEl("text", {
            class: "value",
            x: pad.left + full + 8,
            y: y + 13,
            text: `${value} / ${group.runs[key] || 0}`,
          }),
        );
      });
      cursor += rowHeight + groupGap;
    }
    return h("div", { class: "chart" }, svg);
  };

  /* Spend per model, on the same bars and the same arm/best ratio the headline
   * uses. Ratios are within a model: across models they would compare provider
   * list prices rather than the interfaces. */
  const costChart = () => {
    const chart = view.charts.cost_bars;
    return h(
      "div",
      { class: "headline cost-groups" },
      chart.series.map((item) => {
        const best = Object.entries(item.ratios).find(([, r]) => r === 1)?.[0] ?? null;
        return h(
          "div",
          { class: "headline-row" },
          h(
            "div",
            { class: "headline-head" },
            h("h3", { text: item.model }),
            item.billed_currency && item.billed_currency !== "USD"
              ? h("span", { class: "caption", text: t("cost.billed_in", { currency: item.billed_currency }) })
              : null,
          ),
          h(
            "div",
            { class: "bars" },
            view.treatments.map((key) => {
              const value = item.values[key];
              const ratio = item.ratios[key];
              return h(
                "div",
                { class: item.estimable ? "bar-row" : "bar-row cost-amount", "data-best": String(key === best) },
                h("span", { class: "bar-name", text: treatment(key) }),
                item.estimable ? h(
                  "div",
                  { class: "bar-track" },
                  isNum(value)
                    ? h("div", {
                        class: "bar-fill",
                        "data-arm": key,
                        style: `width:${Math.max(1, (item.fractions[key] || 0) * 100)}%`,
                      })
                    : null,
                ) : null,
                h(
                  "div",
                  { class: "bar-value" },
                  // Two decimals, as in the headline; the exact figure is in the table below.
                  h("strong", { text: item.bounds_labels ? item.bounds_labels[key] : isNum(value) ? `${item.currency} ${num(value, 2)}` : NA() }),
                  isNum(ratio) ? h("span", {
                    class: "bar-ratio",
                    text: `\u00d7${ratio.toFixed(2)}`,
                  }) : null,
                ),
              );
            }),
          ),
          item.estimate_note
            ? h("p", { class: "caption" }, item.estimate_note[language])
            : item.estimable
            ? null
            : h("p", { class: "caption", text: t("cost.not_estimable") }),
        );
      }),
    );
  };

  const capabilityChart = () => {
    const rankings = view.charts.capability_bars.rankings;
    const keys = ["overall", ...view.treatments.filter((key) => key in rankings)];
    return h(
      "div",
      { class: "grid-4" },
      keys
        .filter((key) => key in rankings)
        .map((key) => {
          const entries = rankings[key];
          const rows = entries.map((entry) =>
            h(
              "div",
              { style: "display:grid;grid-template-columns:1fr auto;gap:4px;margin-top:8px" },
              h("span", { class: "caption", text: entry.model }),
              h("span", { class: "num", text: num(entry.score, 2) }),
              h(
                "div",
                { style: "grid-column:1/-1;height:6px;border-radius:999px;background:var(--rule)" },
                h("div", {
                  style: `height:6px;border-radius:999px;background:var(--ink);width:${Math.max(0, Math.min(100, entry.score))}%`,
                }),
              ),
            ),
          );
          return h(
            "div",
            { class: "card" },
            h("h3", { text: key === "overall" ? t("models.overall") : treatment(key) }),
            rows,
          );
        }),
    );
  };

  // --- sections -----------------------------------------------------------

  /* Each section opens with the finding it belongs to, so a reader who scrolled
   * past the overview still knows what the charts under it are evidence for. */
  const sectionInsight = (id) =>
    h("p", { class: "insight", text: narrative().takeaways[id].headline });

  const interfaceSection = () =>
    section(
      "interface",
      t("interface.title"),
      t("interface.lede"),
      sectionInsight("one_store"),
      ...relativeBars("storage_shape"),
      h("p", { class: "note", text: t("interface.rows_note") }),
      panel(t("panel.strips"), ...strips("storage_shape")),
      h("h3", { style: "margin-top:28px", text: t("diagnosis.title") }),
      h("p", { class: "caption", text: t("diagnosis.lede", { runs: view.charts.diagnosis_slope.runs_per_treatment }) }),
      slopeChart(),
      h("h3", { style: "margin-top:28px", text: t("cost.title") }),
      h("p", { class: "caption", text: t("cost.lede") }),
      costChart(),
      h("p", { class: "note", text: narrative().cost_direction.storage_shape }),
      panel(t("panel.cost"), treatmentCostTable()),
    );

  const semanticSection = () =>
    section(
      "semantic",
      t("semantic.title"),
      t("semantic.lede"),
      sectionInsight("semantic_layer"),
      ...relativeBars("semantic_layer"),
      h("p", { class: "note", text: narrative().cost_direction.semantic_layer }),
      panel(t("panel.strips"), ...strips("semantic_layer")),
      h("h3", { style: "margin-top:28px", text: t("semantic.reversal_title") }),
      h("p", { class: "caption", text: t("semantic.reversal_lede") }),
      sectionInsight("fault_dependent"),
      accuracyByLevel(),
      h("div", { class: "scope-diagnosis" },
        h("h3", { text: view.charts.component_dependency_slope.label[language] }),
        h("p", { class: "caption", text: t("diagnosis.lede", { runs: view.charts.component_dependency_slope.runs_per_treatment }) }),
        slopeChart(view.charts.component_dependency_slope),
      ),
      // The same runs split by source, published because the two splits are
      // collinear here and the reader has to be able to see that.
      panel(
        t("panel.confound"),
        h("ul", { class: "caption plain" }, narrative().dataset_reversal.map((text) => h("li", { text }))),
        reversalChart(view.charts.diagnosis_by_dataset),
      ),
      h("h3", { style: "margin-top:28px", text: t("semantic.mechanism_title") }),
      h("p", { class: "caption", text: narrative().mechanism_summary }),
      mechanismGrid(),
      panel(t("panel.mechanism"), mechanismTable()),
      panel(t("panel.case_effects"), caseEffectTable()),
    );

  const mechanismGrid = () => {
    const mechanisms = [];
    const byMechanism = new Map();
    for (const model of view.models) {
      for (const item of data.model_reports[model].transfer.mechanism_effects) {
        if (!byMechanism.has(item.mechanism_code)) {
          byMechanism.set(item.mechanism_code, new Map());
          mechanisms.push(item.mechanism_code);
        }
        const rows = item.metrics.rows_returned;
        byMechanism
          .get(item.mechanism_code)
          .set(model, rows.eligible_cases ? rows.case_median_delta : null);
      }
    }
    return table(
      [t("th.mechanism"), ...view.models.map((model) => ({ label: model, numeric: true }))],
      mechanisms.map((mechanism) => [
        mechanismLabel(mechanism),
        ...view.models.map((model) => deltaCell(byMechanism.get(mechanism).get(model), 2)),
      ]),
    );
  };

  // "CPU saturation" and "Disk I/O degradation" cannot be recovered from the
  // code by casing rules, so the labels come from the view model.
  const mechanismLabel = (code) => {
    const label = view.mechanism_labels[code];
    return label ? label[language] || label.en : code;
  };

  const scopeLabel = (code) => {
    const label = view.scope_labels[code];
    return label ? label[language] || label.en : code;
  };

  const mechanismTable = () =>
    table(
      [
        t("th.model"),
        t("th.mechanism"),
        { label: t("th.eligible_cases"), numeric: true },
        { label: t("th.rows"), numeric: true },
        { label: t("th.calls"), numeric: true },
      ],
      view.models.flatMap((model) =>
        data.model_reports[model].transfer.mechanism_effects.map((item) => [
          model,
          mechanismLabel(item.mechanism_code),
          { value: item.metrics.rows_returned.eligible_cases, numeric: true },
          deltaCell(item.metrics.rows_returned.case_median_delta, 2),
          deltaCell(item.metrics.correct_completion_tool_calls.case_median_delta, 2),
        ]),
      ),
    );

  const caseEffectTable = () =>
    table(
      [
        t("th.model"),
        t("th.case"),
        t("th.mechanism"),
        { label: t("th.rows"), numeric: true },
        { label: t("th.calls"), numeric: true },
        { label: t("th.input"), numeric: true },
        { label: t("th.output"), numeric: true },
        { label: t("th.cost"), numeric: true },
      ],
      view.models.flatMap((model) =>
        data.model_reports[model].transfer.case_effects.map((item) => [
          model,
          item.case_id.replace("semantic-rca-transfer-", ""),
          mechanismLabel(item.mechanism_code),
          deltaCell(item.rows_returned, 2),
          deltaCell(item.correct_completion_tool_calls, 2),
          deltaCell(item.provider_visible_input_tokens, 0),
          deltaCell(item.output_tokens, 0),
          deltaCell(item.estimated_cost, 4),
        ]),
      ),
    );

  const retrievalSection = () => {
    const rows = [];
    for (const model of view.models) {
      const benchmarks = data.model_reports[model].micro.benchmarks;
      for (const [name, summary] of Object.entries(benchmarks)) {
        const effect = summary.case_level_effect;
        const label = view.benchmark_labels[name];
        rows.push([
          model,
          (label && (label[language] || label.en)) || name,
          { value: effect.rows_returned_through_evidence.eligible_cases, numeric: true },
          deltaCell(effect.rows_returned_through_evidence.median_delta, 2),
          {
            value: `${effect.rows_returned_through_evidence.improvements} / ${effect.rows_returned_through_evidence.ties} / ${effect.rows_returned_through_evidence.regressions}`,
            numeric: true,
          },
          deltaCell(effect.tool_calls_through_evidence.median_delta, 2),
          deltaCell(effect.reported_total_tokens.median_delta, 0),
        ]);
      }
    }
    const task = (key) =>
      h(
        "div",
        { class: "card" },
        h("h3", { text: t(`retrieval.${key}.title`) }),
        h("p", { text: t(`retrieval.${key}.body`) }),
      );
    return section(
      "retrieval",
      t("retrieval.title"),
      t("retrieval.lede"),
      h("div", { class: "grid-2" }, task("discovery"), task("graph")),
      h("p", { class: "insight", text: narrative().micro_summary }),
      h("p", { class: "section-lede", text: t("retrieval.interpretation") }),
      table(
        [
          t("th.model"),
          t("th.task"),
          { label: t("th.eligible_cases"), numeric: true },
          { label: t("th.rows"), numeric: true },
          { label: t("th.direction"), numeric: true },
          { label: t("th.calls"), numeric: true },
          { label: t("th.input"), numeric: true },
        ],
        rows,
      ),
      h("p", { class: "caption", text: t("retrieval.metric_note") }),
    );
  };

  const modelsSection = () => {
    const facts = view.facts;
    const cards = view.models.map((model) => {
      const report = data.model_reports[model];
      const transfer = report.transfer;
      const configuration = report.configuration;
      return h(
        "div",
        { class: "card" },
        h("h3", { text: model }),
        h("p", { class: "caption", text: `${configuration.provider} · ${configuration.reasoning_effort} · ${configuration.api_transport}` }),
        definitions([
          [t("th.diagnosis"), view.treatments.map((key) => `${treatment(key)} ${transfer.diagnosis_correct[key]}`).join(" · ")],
          [t("th.eligible_cases"), view.treatments.map((key) => `${treatment(key)} ${transfer.efficiency_eligibility.by_treatment[key]}`).join(" · ")],
          [t("th.failed_queries"), num(transfer.reliability.failed_database_queries)],
          [t("th.total_cost"), money(data.costs.models[model].estimated_cost, data.costs.models[model].currency)],
        ]),
      );
    });
    return section(
      "models",
      t("models.title"),
      null,
      h("h3", { text: t("models.score_title") }),
      h("p", {
        class: "caption",
        text: t("models.score_note", {
          max: facts.capability_rubric_maximum,
          split: facts.capability_rubric_label,
          runs: facts.runs_per_model,
        }),
      }),
      capabilityChart(),
      narrative().citation_submission
        ? h("p", { class: "note", text: narrative().citation_submission })
        : null,
      h("div", { class: "grid-4", style: "margin-top:24px" }, cards),
      panel(t("panel.scores"), capabilityTable(), rubricTable()),
    );
  };

  const capabilityTable = () => {
    const models = data.capability_scores.models;
    return table(
      [
        t("th.model"),
        { label: t("models.overall"), numeric: true },
        ...view.treatments.map((key) => ({ label: treatment(key), numeric: true })),
      ],
      view.models.map((model) => [
        model,
        { value: num(models[model].overall.normalized_score, 2), numeric: true },
        ...view.treatments.map((key) => ({
          value: models[model].by_treatment[key] ? num(models[model].by_treatment[key].normalized_score, 2) : NA(),
          numeric: true,
        })),
      ]),
    );
  };

  const rubricTable = () =>
    table(
      [t("th.dimension"), t("th.metric"), { label: t("th.points"), numeric: true }],
      Object.entries(data.capability_scores.rubric).map(([, contract]) => [
        contract.dimension,
        contract.label,
        { value: contract.points, numeric: true },
      ]),
    );

  const casesSection = () =>
    section(
      "cases",
      t("cases.title"),
      t("cases.lede"),
      panel(
        t("cases.catalog"),
        table(
          [t("th.case"), t("th.dataset"), t("th.system"), t("th.scope"), t("th.target"), { label: t("th.samples"), numeric: true }],
          data.case_catalog.map((item) => [
            item.case_id.replace("semantic-rca-transfer-", ""),
            item.dataset,
            item.system,
            `${scopeLabel(item.causal_scope)} · ${mechanismLabel(item.mechanism_code)}`,
            item.target,
            {
              value: item.oracle ? `${num(item.oracle.normal_samples)} / ${num(item.oracle.abnormal_samples)}` : NA(),
              numeric: true,
              class: item.oracle ? null : "na",
            },
          ]),
        ),
      ),
      panel(
        t("cases.outcomes"),
        table(
          [
            t("th.case"),
            t("th.mechanism"),
            ...view.treatments.map((key) => ({ label: treatment(key), numeric: true })),
            { label: t("th.eligible_models"), numeric: true },
          ],
          data.case_outcomes.map((item) => [
            item.case_id.replace("semantic-rca-transfer-", ""),
            mechanismLabel(item.mechanism_code),
            ...view.treatments.map((key) => ({ value: item.diagnosis_correct[key], numeric: true })),
            { value: item.eligible_models, numeric: true },
          ]),
        ),
      ),
    );

  const resourcesSection = () =>
    section(
      "resources",
      t("resources.title"),
      t("resources.lede"),
      table(
        [
          t("th.model"),
          { label: t("th.provider_input"), numeric: true },
          { label: t("th.uncached"), numeric: true },
          { label: t("th.cache_read"), numeric: true },
          { label: t("th.cache_write"), numeric: true },
          { label: t("th.output"), numeric: true },
          { label: t("th.reasoning"), numeric: true },
        ],
        view.models.map((model) => {
          const usage = data.model_reports[model].usage.combined;
          return [
            model,
            { value: num(usage.provider_visible_input_tokens), numeric: true },
            { value: num(usage.uncached_input_tokens), numeric: true },
            { value: num(usage.cache_read_input_tokens), numeric: true },
            { value: num(usage.cache_creation_input_tokens), numeric: true },
            { value: num(usage.output_tokens), numeric: true },
            {
              value: isNum(usage.reasoning_output_tokens) && usage.reasoning_output_tokens > 0 ? num(usage.reasoning_output_tokens) : NA(),
              numeric: true,
              class: usage.reasoning_output_tokens ? null : "na",
            },
          ];
        }),
      ),
      h("div", {},
        h("h3", { text: t("pricing.title") }),
        ...view.charts.cost_bars.series.filter((item) => item.estimate_note).map((item) =>
          h("p", { class: "caption", text: `${item.model}: ${item.estimate_note[language]}` }),
        ),
        pricingTable(),
        h("h4", { text: t("pricing.exchange_rates") }),
        table(
          [t("th.model"), t("th.currency"), t("pricing.units_per_usd"), t("pricing.verified")],
          view.charts.cost_bars.converted.map((item) => [
            item.model, item.currency, item.units_per_usd,
            h("a", { href: item.source, text: item.checked_at }),
          ]),
        ),
      ),
      panel(
        t("panel.reliability"),
        h("h4", { text: t("th.runs") }),
        table(
          [t("th.model"), { label: t("th.runs"), numeric: true }, { label: t("th.failed_queries"), numeric: true }, { label: t("th.runner_errors"), numeric: true }, { label: t("th.budget"), numeric: true }],
          view.models.map((model) => {
            const reliability = data.model_reports[model].reliability;
            return [
              model,
              { value: num(reliability.runs), numeric: true },
              { value: num(reliability.failed_database_queries), numeric: true },
              { value: num(reliability.runner_errors), numeric: true },
              { value: num(reliability.budget_exhaustions), numeric: true },
            ];
          }),
        ),
        h("h4", { text: t("th.total_cost") }),
        costTable(),
      ),
    );

  /* Correct diagnoses, then how many of those runs also cleared the citation and
   * reliability conditions. A gap between the two columns is the eligibility loss. */
  const eligibilityTable = () =>
    table(
      [
        t("th.model"),
        ...view.treatments.flatMap((key) => [
          { label: `${treatment(key)} ${t("th.correct").toLowerCase()}`, numeric: true },
          { label: `${treatment(key)} ${t("th.eligible_cases").toLowerCase()}`, numeric: true },
        ]),
      ],
      view.models.map((model) => {
        const transfer = data.model_reports[model].transfer;
        return [
          model,
          ...view.treatments.flatMap((key) => [
            { value: transfer.diagnosis_correct[key], numeric: true },
            {
              value: transfer.efficiency_eligibility.by_treatment[key],
              numeric: true,
              class:
                transfer.efficiency_eligibility.by_treatment[key] < transfer.diagnosis_correct[key]
                  ? "pos"
                  : null,
            },
          ]),
        ];
      }),
    );

  const evidenceQualityTable = () =>
    table(
      [
        t("th.model"),
        ...Object.keys(data.model_reports[view.models[0]].transfer.evidence_quality.paired_disposition).map(
          (key) => ({ label: key.split("_").join(" "), numeric: true }),
        ),
      ],
      view.models.map((model) => {
        const disposition = data.model_reports[model].transfer.evidence_quality.paired_disposition;
        return [model, ...Object.values(disposition).map((value) => ({ value, numeric: true }))];
      }),
    );

  /* Why a claim was not accepted, per arm. Published because the reasons decide
   * eligibility, and eligibility decides which cases reach the endpoints. */
  /* Split by whether the citation claimed the mechanism at all. The verifier
   * checks every citation, so a run's raw code count is far larger than the
   * number of checks it actually attempted. */
  const rejectionCodeTable = () => {
    const audit = data.claim_rejection_audit;
    const rows = Object.entries(audit.by_code)
      .sort((a, b) => b[1].claiming - a[1].claiming)
      .map(([code, counts]) => [
        code,
        { value: counts.claiming, numeric: true },
        { value: counts.not_claiming, numeric: true, class: "na" },
      ]);
    return h(
      "div",
      {},
      h("p", { class: "caption", text: t("method.rejection_note") }),
      table(
        [t("th.metric"), t("th.rejection_claiming"), t("th.rejection_other")],
        rows,
      ),
    );
  };

  const costTable = () =>
    table(
      [t("th.model"), t("th.currency"), { label: t("th.total_cost"), numeric: true }],
      view.models.map((model) => {
        const entry = data.costs.models[model];
        return [
          model,
          entry.currency,
          {
            value: isNum(entry.estimated_cost) ? num(entry.estimated_cost, 4) : NA(),
            numeric: true,
            class: isNum(entry.estimated_cost) ? null : "na",
          },
        ];
      }),
    );

  const treatmentCostTable = () =>
    table(
      [t("th.model"), ...view.treatments.map((key) => ({ label: treatment(key), numeric: true }))],
      view.charts.cost_bars.series.map((item) => [
        item.model,
        ...view.treatments.map((key) => ({
          value: item.bounds_labels ? item.bounds_labels[key] : isNum(item.values[key]) ? money(item.values[key], item.currency) : NA(),
          numeric: true,
          class: item.bounds_labels || isNum(item.values[key]) ? null : "na",
        })),
      ]),
    );

  const RATE_KEYS = [
    ["uncached_input_per_million", "th.uncached"],
    ["cache_read_per_million", "th.cache_read"],
    ["cache_write_per_million", "th.cache_write"],
    ["output_per_million", "th.output"],
  ];
  const pricingTable = () => {
    return table(
      [
        t("th.model"),
        t("th.currency"),
        ...RATE_KEYS.map(([, key]) => ({ label: t(key), numeric: true })),
        t("th.value"),
      ],
      view.charts.pricing_basis.map((basis) => [
        basis.model,
        basis.currency,
        ...RATE_KEYS.map(([key]) => ({
          value: isNum(basis.rates[key]) ? num(basis.rates[key], 2) : NA(),
          numeric: true,
          class: isNum(basis.rates[key]) ? null : "na",
        })),
        h("a", { href: basis.source, text: basis.checked_at }),
      ]),
    );
  };

  const methodSection = () => {
    const scope = data.scope;
    const audit = data.audit;
    const toolAudit = data.tool_use_audit;
    return section(
      "method",
      t("method.title"),
      null,
      h("h3", { text: t("method.scope") }),
      definitions([
        ["Protocol", scope.protocol_revision],
        ["GreptimeDB", `${scope.greptimedb_revision.slice(0, 12)} · ${scope.greptimedb_build_profile}`],
        ["Estimand", scope.treatment_estimand],
        [t("th.runs"), `${num(data.execution.completed_cells)} / ${num(data.execution.expected_cells)}`],
      ]),
      h("h3", { style: "margin-top:24px", text: t("method.tool_use") }),
      h("p", { class: "caption", text: narrative().tool_use }),
      table(
        [
          t("th.treatment"),
          { label: t("th.runs"), numeric: true },
          { label: t("label.join_calls"), numeric: true },
          { label: t("label.join_runs"), numeric: true },
          { label: t("label.promql_runs"), numeric: true },
        ],
        Object.entries(toolAudit.by_treatment).map(([key, bucket]) => [
          treatment(key),
          { value: bucket.runs, numeric: true },
          { value: bucket.successful_sql_join_calls, numeric: true },
          { value: bucket.runs_with_successful_sql_join, numeric: true },
          { value: bucket.runs_with_successful_promql_evaluation, numeric: true },
        ]),
      ),
      panel(
        t("panel.tool_calls"),
        table(
          [t("th.treatment"), t("th.tool"), { label: t("th.calls_total"), numeric: true }, { label: t("th.calls_ok"), numeric: true }],
          Object.entries(toolAudit.by_treatment).flatMap(([key, bucket]) =>
            Object.keys(bucket.tool_calls).map((tool) => [
              treatment(key),
              tool,
              { value: bucket.tool_calls[tool], numeric: true },
              { value: bucket.successful_tool_calls[tool] || 0, numeric: true },
            ]),
          ),
        ),
      ),
      h("h3", { style: "margin-top:24px", text: t("method.eligibility") }),
      eligibilityTable(),
      h("h3", { style: "margin-top:24px", text: t("method.evidence") }),
      h("p", { class: "caption", text: t("method.evidence_note") }),
      table(
        [t("th.model"), { label: t("th.covered"), numeric: true }, { label: t("th.failed"), numeric: true }, { label: t("th.not_estimable"), numeric: true }],
        view.models.map((model) => {
          const unscored = data.capability_scores.models[model].overall.unscored_dimensions.required_evidence_covered;
          return [
            model,
            { value: unscored.covered || 0, numeric: true },
            { value: unscored.failed || 0, numeric: true },
            { value: unscored.not_estimable || 0, numeric: true },
          ];
        }),
      ),
      panel(t("panel.evidence"), evidenceQualityTable(), rejectionCodeTable()),
      h("h3", { style: "margin-top:24px", text: t("method.inference") }),
      table(
        ["Protocol", t("th.model"), "m (Holm)"],
        view.inference_groups.map((group) => [
          group.protocol_revision, group.models.join(", "), group.family_size,
        ]),
      ),
      definitions(view.verdicts.filter((item) => item.endpoints).map((item) => [
        item.comparison, t(`role.${item.role}`),
      ])),
      definitions(
        Object.entries(scope.inference)
          .filter(([key, value]) => key !== "policy" && typeof value !== "object")
          .map(([key, value]) => [key.split("_").join(" "), String(value)]),
      ),
      h("h3", { style: "margin-top:24px", text: t("method.audit") }),
      view.split_rerun_note
        ? h("p", { class: "caption", text: view.split_rerun_note[language] })
        : null,
      view.execution_deviation_note
        ? h("p", { class: "caption", text: view.execution_deviation_note[language] })
        : null,
      definitions(Object.entries(audit).map(([key, value]) => [key.split("_").join(" "), String(value)])),
      h("h3", { style: "margin-top:24px", text: t("method.limits") }),
      h("ul", { class: "plain" }, data.limitations.map((item) => h("li", { text: item }))),
      h("h3", { style: "margin-top:24px", text: t("method.glossary") }),
      definitions(
        ["run", "arms", "eligible", "median", "delta", "holm", "na"].map((key) => [
          t(`glossary.${key}`),
          t(`glossary.${key}.def`),
        ]),
      ),
      h("h3", { style: "margin-top:24px", text: t("method.attribution") }),
      h("p", { text: narrative().attribution_text }),
      h("p", { class: "caption", text: narrative().attribution_terms }),
      h(
        "p",
        {},
        view.attribution_links.map((link, index) => [
          index ? " · " : "",
          h("a", { href: link.url, text: link.label }),
        ]),
      ),
      h("h3", { style: "margin-top:24px", text: t("method.artifacts") }),
      definitions([
        [t("th.value"), h("code", { text: data.integrity.semantic_payload_sha256 })],
        ...Object.entries(data.source_artifacts).map(([key, artifact]) => [
          key,
          h("code", { text: artifact.sha256 }),
        ]),
      ]),
    );
  };

  const chrome = () =>
    h(
      "div",
      { class: "topbar" },
      h(
        "div",
        { class: "wrap topbar-inner" },
        h(
          "nav",
          { "aria-label": t("nav.overview") },
          SECTIONS.map((id) => h("a", { href: `#${language}-${id}`, text: t(`nav.${id}`) })),
        ),
        h(
          "div",
          { class: "lang", role: "group" },
          LANGUAGES.map((code) =>
            h("button", {
              type: "button",
              "data-language": code,
              "aria-pressed": String(code === language),
              text: t(`lang.${code}`),
            }),
          ),
        ),
      ),
    );

  const footer = () =>
    h(
      "footer",
      {},
      h(
        "div",
        { class: "wrap" },
        h("p", { text: t("footer.text") }),
        view.facts.publication ? h("p", { text: [
          t("hero.meta.measurement_updated", { timestamp: view.facts.publication.measurement_updated_at }),
          t("hero.meta.report_generated", { timestamp: view.facts.publication.report_generated_at }),
        ].join(" · ") }) : null,
        h("p", {}, h("a", {
          href: "https://github.com/GreptimeTeam/agent-rca-bench/blob/main/REPORT.md",
          text: t("footer.report"),
        })),
      ),
    );

  // --- shell --------------------------------------------------------------

  const render = () => {
    const root = document.getElementById("report");
    root.textContent = "";
    root.hidden = false;
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.getElementById("fallback")?.remove();
    root.append(
      chrome(),
      heroSection(),
      h(
        "main",
        {},
        overviewSection(),
        interfaceSection(),
        semanticSection(),
        retrievalSection(),
        modelsSection(),
        casesSection(),
        resourcesSection(),
        questionsSection(),
        methodSection(),
      ),
      footer(),
    );
    // Section ids carry the language so a deep link keeps both.
    for (const id of SECTIONS) {
      const node = document.getElementById(id);
      if (node) node.id = `${language}-${id}`;
    }
    root.querySelectorAll("[data-language]").forEach((button) => {
      button.addEventListener("click", () => {
        const next = button.dataset.language;
        if (next === language) return;
        const current = parseHash();
        window.location.hash = `#${next}${current.section ? `-${current.section}` : ""}`;
      });
    });
  };

  const parseHash = () => {
    const match = window.location.hash.match(/^#(en|zh)(?:-(.+))?$/);
    return match ? { language: match[1], section: match[2] || null } : { language: null, section: null };
  };

  const apply = () => {
    const state = parseHash();
    const next = LANGUAGES.includes(state.language) ? state.language : "en";
    if (next !== language || !document.getElementById("report").hasChildNodes()) {
      language = next;
      render();
    }
    if (state.section) {
      requestAnimationFrame(() => {
        document.getElementById(`${language}-${state.section}`)?.scrollIntoView();
      });
    }
  };

  window.addEventListener("hashchange", apply);
  apply();
})();
