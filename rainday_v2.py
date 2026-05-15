import pandas as pd
import requests
from datetime import date
from constants import API_KEY


def get_snapshot_map(history_slice, heavy_thr=3.0, insane_thr=7.0):
    """
    Returns a dict mapping stationId to an integer severity:
    0 = Ignored/Dry, 1 = Heavy (>= 3.0mm/5min), 2 = Torrential (>= 7.0mm/5min)

    A station only drops back to 0 if the current AND two preceding readings
    are all below heavy_thr (~10 mins of sustained low rain).
    """
    if not history_slice:
        return {}

    latest = history_slice[0]
    persistence = {}

    for entry in latest.get('data', []):
        sid = entry['stationId']
        val = entry['value']
        persistence[sid] = {'current': val, 'prev1': val, 'prev2': val}

    if len(history_slice) > 1:
        for entry in history_slice[1].get('data', []):
            sid = entry['stationId']
            if sid in persistence:
                persistence[sid]['prev1'] = entry['value']

    if len(history_slice) > 2:
        for entry in history_slice[2].get('data', []):
            sid = entry['stationId']
            if sid in persistence:
                persistence[sid]['prev2'] = entry['value']

    status_map = {}
    for sid, info in persistence.items():
        curr  = info['current']
        prev1 = info['prev1']
        prev2 = info['prev2']

        if curr >= insane_thr:
            status_map[sid] = 2
        elif curr >= heavy_thr or prev1 >= heavy_thr or prev2 >= heavy_thr:
            status_map[sid] = 1
        else:
            status_map[sid] = 0

    return status_map


def merge_nearby_spans(runs, merge_gap=2):
    """
    runs: list of {'span': (start, end), 'severity': int}, sorted by span start.
    Merges any two adjacent runs whose gap is <= merge_gap.
    Severity of merged span is the max of the two.
    """
    if not runs:
        return []

    merged = [runs[0].copy()]
    for current in runs[1:]:
        last = merged[-1]
        gap = current['span'][0] - last['span'][1] - 1
        if gap <= merge_gap:
            merged[-1] = {
                'span': (last['span'][0], current['span'][1]),
                'severity': max(last['severity'], current['severity'])
            }
        else:
            merged.append(current.copy())

    return merged


def get_road_spans(lookup_table, rain_map, merge_gap=2):
    """
    Returns dict: {road: [{'span': (start_idx, end_idx), 'severity': int}, ...]}
    Each entry is a merged contiguous wet zone on that road.
    """
    spans = {}
    for road, group in lookup_table.groupby('road'):
        severities = group.apply(
            lambda x: max(rain_map.get(x['st_a'], 0), rain_map.get(x['st_b'], 0)), axis=1
        )

        wet_indices = sorted(group.index[severities > 0].tolist())
        if not wet_indices:
            continue

        # Build contiguous runs
        runs = []
        run_start = run_end = wet_indices[0]
        run_sev = severities[run_start]

        for idx in wet_indices[1:]:
            if idx == run_end + 1:
                run_end = idx
                run_sev = max(run_sev, severities[idx])
            else:
                runs.append({'span': (run_start, run_end), 'severity': run_sev})
                run_start = run_end = idx
                run_sev = severities[idx]
        runs.append({'span': (run_start, run_end), 'severity': run_sev})

        spans[road] = merge_nearby_spans(runs, merge_gap)

    return spans


def overlaps(span_a, span_b):
    return span_a[0] <= span_b[1] and span_b[0] <= span_a[1]


def prior_boundary(matched_spans):
    """
    Given a list of matched previous span dicts, return the
    union (min_start, max_end) as a single boundary tuple.
    """
    if not matched_spans:
        return None
    return (
        min(s['span'][0] for s in matched_spans),
        max(s['span'][1] for s in matched_spans)
    )


def process_stateless_alerts(rain_history, lookup_table="lookup.csv"):
    if len(rain_history) < 3:
        return []

    lookup_table = pd.read_csv(lookup_table, low_memory=False)

    # Compute all span snapshots once — no duplicate calls
    all_spans = [
        get_road_spans(lookup_table, get_snapshot_map(rain_history[i:i+3]))
        for i in range(5)
    ]

    curr_spans     = all_spans[0]
    prev_5m_spans  = all_spans[1]
    prev_10m_spans = all_spans[2]
    hwm_history    = all_spans[1:5]  # [2] is shared with prev_10m_spans

    alerts = []

    for road, road_spans in curr_spans.items():
        for curr in road_spans:
            c_span = curr['span']
            c_sev  = curr['severity']

            sev_text   = "🟣 TORRENTIAL" if c_sev == 2 else "🔴 Heavy"
            start_name = lookup_table.loc[c_span[0], 'segment_name'].split(' to ')[0]
            end_name   = lookup_table.loc[c_span[1], 'segment_name'].split(' to ')[-1]

            p5_matches  = [s for s in prev_5m_spans.get(road, [])  if overlaps(c_span, s['span'])]
            p10_matches = [s for s in prev_10m_spans.get(road, []) if overlaps(c_span, s['span'])]

            was_raining_recently = bool(p5_matches or p10_matches)

            # TYPE 1: New Rain
            if not was_raining_recently:
                alerts.append({
                    "type": "NEW",
                    "road": road,
                    "message": f"{sev_text} rain detected on {road} between {start_name} and {end_name}."
                })
                continue

            max_past_sev = max((s['severity'] for s in p5_matches + p10_matches), default=0)

            # TYPE 4: Escalation
            if c_sev == 2 and max_past_sev < 2:
                alerts.append({
                    "type": "ESCALATION",
                    "road": road,
                    "message": f"🟣 Rain on {road} between {start_name} and {end_name} has intensified to TORRENTIAL levels."
                })
                continue

            # TYPE 3: Expansion (using union of all matching p10 spans as prior boundary)
            p10_boundary = prior_boundary(p10_matches)
            if p10_boundary:
                road_hwm = [h.get(road, []) for h in hwm_history]
                all_hwm_spans = [s for road_spans in road_hwm for s in road_spans
                                 if overlaps(c_span, s['span'])]

                if all_hwm_spans:
                    hwm_start = min(s['span'][0] for s in all_hwm_spans)
                    hwm_end   = max(s['span'][1] for s in all_hwm_spans)

                    expansion_vectors = []

                    if c_span[1] > p10_boundary[1] and c_span[1] > hwm_end:
                        origin = lookup_table.loc[p10_boundary[1], 'segment_name'].split(' to ')[-1]
                        target = lookup_table.loc[c_span[1], 'segment_name'].split(' to ')[-1]
                        expansion_vectors.append(f"from {origin} towards {target}")

                    if c_span[0] < p10_boundary[0] and c_span[0] < hwm_start:
                        origin = lookup_table.loc[p10_boundary[0], 'segment_name'].split(' to ')[0]
                        target = lookup_table.loc[c_span[0], 'segment_name'].split(' to ')[0]
                        expansion_vectors.append(f"from {origin} towards {target}")

                    if expansion_vectors:
                        alerts.append({
                            "type": "MOVING",
                            "road": road,
                            "message": f"⏩ {sev_text} rain on {road} is moving {' and '.join(expansion_vectors)}."
                        })

    return alerts


def run_rainday():
    rainday = []

    s = requests.Session()
    s.headers.update({'x-api-key': API_KEY})
    paginationToken = ""

    n = 1
    latest_only = True

    today = date.today().isoformat()

    while True:
        url = f"https://api-open.data.gov.sg/v2/real-time/api/rainfall?date={today}"
        if paginationToken:
            url += ("&paginationToken=" + paginationToken)
        res = s.get(url=url)
        res_json = res.json()['data']
        if not res_json: return None
        rainday.append(res_json)
        if 'paginationToken' not in res_json.keys() or latest_only:
            break
        paginationToken = res_json['paginationToken']
        n += 1

    alerts = process_stateless_alerts(rainday[0]['readings'])
    return alerts