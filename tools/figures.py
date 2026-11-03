#!/usr/bin/env python3
"""Figures for README and REPORT, written as plain SVG so GitHub can show them. Needs no network.

`regimes.svg` is a schematic, not data: the same two probes reach the leader at the two moments the design uses. In the
buffered regime both wait for slot F and the sort decides, in the window regime the leader is already producing and takes
them as they come. The motion is CSS inside the SVG, because GitHub strips scripts but keeps an image's own styles. The
resting frame is the last frame, so a reader with reduced motion, or a viewer that skips animation, sees the outcome.

`curve.svg` is data, from report/P/summary.json: per validator, how often Y landed before A when the leader held both (C4) and
when Y was sent 2, 10 or 40 ms after A inside the window (W2, W10, W40), with 95% intervals. It does not move, because the numbers
do not. One dark canvas in the NAVISTRI palette serves both GitHub themes. Usage: figures.py [OUT_DIR] [SUMMARY_JSON].
"""
import json
import os
import sys

NAVY, DEEP, ICE, SKY, MIST = '#152935', '#0c1c25', '#e1faff', '#4ba5c3', '#82aac8'
LINE = 'rgba(225,250,255,.14)'
AMBER, COOL, DIM = '#ffbf5e', '#8fb3cc', '#4f86a3'
MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,monospace'
CHIP_W, CHIP_H, ROW = 40, 20, 24
CYCLE = 8  # seconds


def chip(x, y, label, color):
    return (f'<rect x="{x}" y="{y}" width="{CHIP_W}" height="{CHIP_H}" rx="5" fill="{color}"/>'
            f'<text x="{x + CHIP_W / 2}" y="{y + 14.5}" text-anchor="middle" fill="{NAVY}" font-weight="700">{label}</text>')


def mover(name, final, start, via, label, color, ext):
    """A probe that rests in its block row. The keyframes carry it from the sender through the buffer to that row."""
    (fx, fy), (sx, sy), (vx, vy) = final, start, via
    group = (f'<g transform="translate({fx},{fy})"><g class="mv {name}">{chip(0, 0, label, color)}</g>'
             f'<rect class="fx {ext}" x="{CHIP_W + 4}" y="0" width="104" height="{CHIP_H}" rx="3" fill="{color}" opacity=".28"/></g>')
    return group, (sx - fx, sy - fy), (vx - fx, vy - fy)


def keyframes(name, start, via, sent, at_buffer, leave, rest):
    (sx, sy), (vx, vy) = start, via
    return (f'@keyframes {name}{{0%{{transform:translate({sx}px,{sy}px);opacity:0}}'
            f'{sent}%{{transform:translate({sx}px,{sy}px);opacity:0}}{sent + 3}%{{opacity:1}}'
            f'{at_buffer}%{{transform:translate({vx}px,{vy}px)}}{leave}%{{transform:translate({vx}px,{vy}px)}}'
            f'{rest}%{{transform:translate(0,0)}}88%{{transform:translate(0,0);opacity:1}}96%,100%{{transform:translate(0,0);opacity:0}}}}')


def appear(name, at):
    return f'@keyframes {name}{{0%,{at - 2}%{{opacity:0}}{at + 2}%,88%{{opacity:1}}96%,100%{{opacity:0}}}}'


def lane(top, title, note, block_rows):
    """The fixed parts of one regime: sender, path, the leader's buffer and block F with other senders' transactions."""
    mid = top + 86
    out = [f'<text x="32" y="{top + 24}" fill="{SKY}" font-size="12" letter-spacing="1.5">{title}</text>',
           f'<text x="32" y="{top + 42}" fill="{MIST}" font-size="12">{note}</text>',
           f'<rect x="32" y="{mid - 22}" width="96" height="44" rx="8" fill="{DEEP}" stroke="{LINE}"/>',
           f'<text x="80" y="{mid - 2}" text-anchor="middle" fill="{ICE}">sender</text>',
           f'<text x="80" y="{mid + 14}" text-anchor="middle" fill="{MIST}" font-size="10.5">direct QUIC</text>',
           f'<line x1="132" y1="{mid}" x2="600" y2="{mid}" stroke="{MIST}" stroke-opacity=".5" stroke-dasharray="3 6"/>',
           f'<polygon points="600,{mid - 4} 608,{mid} 600,{mid + 4}" fill="{MIST}" fill-opacity=".6"/>',
           f'<rect x="610" y="{top + 20}" width="318" height="140" rx="10" fill="{DEEP}" stroke="{LINE}"/>',
           f'<rect x="626" y="{top + 48}" width="104" height="96" rx="6" fill="none" stroke="{LINE}" stroke-dasharray="4 4"/>',
           f'<text x="678" y="{top + 40}" text-anchor="middle" fill="{MIST}" font-size="11">buffer</text>',
           f'<text x="836" y="{top + 40}" text-anchor="middle" fill="{MIST}" font-size="11">block F</text>',
           f'<text x="836" y="{top + 154}" text-anchor="middle" fill="{MIST}" font-size="11">…</text>']
    for i, other in enumerate(block_rows):
        if other:
            out.append(f'<rect x="762" y="{top + 48 + i * ROW}" width="148" height="{CHIP_H}" rx="3" fill="{DIM}" opacity=".35"/>')
    return out, mid


def regimes():
    top1, top2 = 64, 252
    parts, css = [], []
    fixed1, mid1 = lane(top1, 'BUFFERED', 'fired at slot F−5, about 1 s before the leader’s first slot', [0, 0, 1, 1])
    fixed2, mid2 = lane(top2, 'INSIDE THE WINDOW', 'fired once slot F has started', [1, 1, 0, 0])
    parts += fixed1 + fixed2
    row = lambda top, i: top + 48 + i * ROW
    buffer_y = lambda top, i: top + 66 + i * 30

    # Buffered: both reach the buffer, wait for slot F, and the sort puts Y on top although it left second.
    for name, label, color, i_buf, i_row, sent, arrive, ext in (('b_a', 'A', COOL, 0, 1, 0, 20, 'xb_a'), ('b_y', 'Y', AMBER, 1, 0, 3, 23, 'xb_y')):
        group, start, via = mover(name, (762, row(top1, i_row)), (136, mid1 - 10), (658, buffer_y(top1, i_buf)), label, color, ext)
        parts.append(group)
        css += [keyframes(name, start, via, sent, arrive, 42, 56), appear(ext, 56)]
    # Window: the leader is producing, so each probe goes into the block the moment it arrives.
    for name, label, color, i_row, sent, arrive, rest, ext in (('w_a', 'A', COOL, 2, 0, 18, 26, 'xw_a'), ('w_y', 'Y', AMBER, 3, 3, 21, 29, 'xw_y')):
        group, start, via = mover(name, (762, row(top2, i_row)), (136, mid2 - 10), (658, buffer_y(top2, 0) + 15), label, color, ext)
        parts.append(group)
        css += [keyframes(name, start, via, sent, arrive, arrive, rest), appear(ext, rest)]

    parts += [f'<text class="fx wait" x="610" y="{top1 + 12}" fill="{SKY}" font-size="11" letter-spacing="1">LEADER  waiting for slot F</text>',
              f'<text class="fx go" x="610" y="{top1 + 12}" fill="{SKY}" font-size="11" letter-spacing="1">LEADER  slot F, producing</text>',
              f'<text x="610" y="{top2 + 12}" fill="{SKY}" font-size="11" letter-spacing="1">LEADER  slot F, producing</text>',
              f'<text class="fx r_b" x="136" y="{mid1 + 44}" fill="{ICE}">Sorted by reward per unit of cost: Y first.</text>',
              f'<text class="fx r_b" x="136" y="{mid1 + 62}" fill="{MIST}" font-size="12">Y left second and still goes first.</text>',
              f'<text class="fx r_w" x="136" y="{mid2 + 44}" fill="{ICE}">Taken as they arrive: A first.</text>',
              f'<text class="fx r_w" x="136" y="{mid2 + 62}" fill="{MIST}" font-size="12">Y overtakes only when both fall into one scheduling batch.</text>',
              f'<line x1="32" y1="244" x2="928" y2="244" stroke="{LINE}"/>',
              chip(32, 428, 'Y', AMBER), f'<text x="80" y="443" fill="{MIST}" font-size="12">more reward per unit of cost</text>',
              chip(318, 428, 'A', COOL), f'<text x="366" y="443" fill="{MIST}" font-size="12">the reference probe</text>',
              f'<text x="570" y="443" fill="{MIST}" font-size="12">Y leaves 10 ms after A, slowed down here</text>']
    css += ['@keyframes wait{0%,40%{opacity:1}44%,100%{opacity:0}}', '@keyframes go{0%,40%{opacity:0}44%,96%{opacity:1}100%{opacity:0}}',
            appear('r_b', 56), appear('r_w', 30)]
    style = (f'text{{font-family:{MONO};font-size:13px}}'
             f'.mv,.fx{{animation-duration:{CYCLE}s;animation-iteration-count:infinite;animation-timing-function:ease-in-out}}'
             + ''.join(f'.{n}{{animation-name:{n}}}' for n in ('b_a', 'b_y', 'w_a', 'w_y', 'xb_a', 'xb_y', 'xw_a', 'xw_y', 'wait', 'go', 'r_b', 'r_w'))
             + '.wait{opacity:0}' + ''.join(css)
             + '@media (prefers-reduced-motion:reduce){.mv,.fx{animation:none}}')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 460" width="960" height="460" role="img">'
            '<title>Same two transactions, two moments of arrival</title>'
            '<desc>In the buffered regime both probes wait in the leader’s buffer and the one with more reward per unit of cost goes '
            'first, though it was sent second. Inside the window the leader is producing and takes them as they arrive, so the one '
            'sent first stays first unless the second lands within the scheduler’s batch.</desc>'
            f'<style>{style}</style><rect width="960" height="460" rx="16" fill="{NAVY}"/>'
            f'<text x="32" y="38" fill="{ICE}" font-size="15" font-weight="600">Same two transactions, two moments of arrival</text>'
            + ''.join(parts) + '</svg>\n')


ROWS = (('own TPU', ('3SkE34PV', '7QQGNm3p', 'ana2y2Yv', 'HEL1USMZ', '9eGrDohd')),
        ('BAM or Harmonic ingress', ('5Cchr1XG', 'DRpbCBMx', '9jxgosAf', 'Fd7btgyS', 'JUPiTERr')))
STEPS = (('C4', 'held'), ('W2', '2'), ('W10', '10'), ('W40', '40 ms'))


def panel(x, y, name, target):
    """One validator: the held point in the context colour, the window points in amber, each with its interval."""
    left, width, top, height = x + 16, 136, y + 40, 80
    px = [left + i * width / 3 for i in range(4)]
    py = lambda share: top + height * (1 - share)
    out = [f'<rect x="{x}" y="{y}" width="168" height="152" rx="8" fill="{DEEP}" stroke="{LINE}"/>',
           f'<text x="{x + 12}" y="{y + 18}" fill="{ICE}" font-size="12">{name}</text>',
           f'<text x="{x + 156}" y="{y + 18}" text-anchor="end" fill="{MIST}" font-size="10.5">{target["version"]}</text>',
           f'<text x="{x + 12}" y="{y + 32}" fill="{MIST}" font-size="10.5">{target["client"]}</text>']
    for share in (0, .5, 1):
        out.append(f'<line x1="{left - 6}" y1="{py(share)}" x2="{left + width + 6}" y2="{py(share)}" stroke="{LINE}"/>')
    cells = [target['cells'].get(kind) for kind, _ in STEPS]
    points = [(px[i], py(c['share'])) for i, c in enumerate(cells) if c and c['n']]
    if len(points) > 1:
        out.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in points[1:])}" fill="none" stroke="{AMBER}" stroke-width="2"/>')
    for i, ((kind, label), c) in enumerate(zip(STEPS, cells)):
        colour = COOL if kind == 'C4' else AMBER
        if c and c['n']:
            out.append(f'<line x1="{px[i]}" y1="{py(c["low"]):.1f}" x2="{px[i]}" y2="{py(c["high"]):.1f}" stroke="{colour}" stroke-opacity=".35" stroke-width="4" stroke-linecap="round"/>')
            out.append(f'<circle cx="{px[i]}" cy="{py(c["share"]):.1f}" r="3.5" fill="{colour}"><title>{kind}: {c["k"]}/{c["n"]}</title></circle>')
        out.append(f'<text x="{px[i]}" y="{y + 140}" text-anchor="middle" fill="{MIST}" font-size="10.5">{label}</text>')
    return out


def curve(summary):
    targets = {identity[:8]: t for identity, t in summary['targets'].items()}
    parts = []
    for row, (label, names) in enumerate(ROWS):
        y = 92 + row * 186
        parts.append(f'<text x="32" y="{y - 10}" fill="{SKY}" font-size="12" letter-spacing="1.5">{label.upper()}</text>')
        for col, name in enumerate(names):
            if name in targets:
                parts += panel(32 + col * 182, y, name, targets[name])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 470" width="960" height="470" role="img">'
            '<title>How often the better transaction goes first, held or arriving later</title>'
            '<desc>For each validator, the share of pairs in which Y landed before A: when the leader held both (C4), and when Y was sent '
            '2, 10 or 40 ms after A while the leader was producing (W2, W10, W40), with 95% intervals.</desc>'
            f'<style>text{{font-family:{MONO};font-size:13px}}</style><rect width="960" height="470" rx="16" fill="{NAVY}"/>'
            f'<text x="32" y="38" fill="{ICE}" font-size="15" font-weight="600">How often Y goes first</text>'
            f'<text x="32" y="58" fill="{MIST}" font-size="12">share of pairs, 0 at a panel’s bottom, 100% at its top</text>'
            f'<circle cx="560" cy="34" r="4" fill="{COOL}"/><text x="572" y="38" fill="{MIST}" font-size="12">held in the buffer</text>'
            f'<circle cx="560" cy="54" r="4" fill="{AMBER}"/><text x="572" y="58" fill="{MIST}" font-size="12">Y sent 2, 10, 40 ms after A, inside the window</text>'
            + ''.join(parts) + '</svg>\n')


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, 'figures')
    source = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, 'report', 'P', 'summary.json')
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, 'regimes.svg'), 'w') as f:
        f.write(regimes())
    print(os.path.join(out, 'regimes.svg'))
    if os.path.exists(source):
        with open(source) as f:
            summary = json.load(f)
        with open(os.path.join(out, 'curve.svg'), 'w') as f:
            f.write(curve(summary))
        print(os.path.join(out, 'curve.svg'))


if __name__ == '__main__':
    main()
