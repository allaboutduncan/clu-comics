/**
 * CLU.renderConstellation(container, graph, options)
 *
 * Draws a graph the server has already laid out (models/reading_insights.py:
 * nodes carry x/y in the unit square). This file only draws and handles
 * pointer and keyboard focus; it computes no layout, so the same history
 * looks the same on every device.
 *
 *   graph    {nodes: [{id, name, count, role, x, y}], edges: [{source, target, weight}]}
 *   options  {hrefFor(node) -> url, unit: 'issue'}
 *
 * A node is a link (to its browse page). Hovering or focusing one lights its
 * ties and dims the rest.
 *
 * Labels: every node is offered one, biggest first, in the first of four
 * places (right, left, below, above) where it covers no other node and no
 * label already placed. A node with no free place stays unlabelled until it
 * is lit, when its name is shown regardless. Labelling a fixed "top twelve"
 * to the right, as this did, wrote names across neighbouring nodes and each
 * other wherever a group was dense.
 */
(function () {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const WIDTH = 1000;
    const HEIGHT = 560;
    // Label metrics for the 13px label font. Width is estimated from the
    // character count: measuring needs the SVG in the document, and an
    // estimate a little on the wide side only costs an occasional label.
    const LABEL_CHAR_WIDTH = 7.4;
    const LABEL_HEIGHT = 17;
    const LABEL_GAP = 5;

    function el(name, attrs) {
        const node = document.createElementNS(SVG_NS, name);
        Object.keys(attrs || {}).forEach(key => node.setAttribute(key, attrs[key]));
        return node;
    }

    window.CLU = window.CLU || {};

    CLU.renderConstellation = function (container, graph, options) {
        options = options || {};
        const unit = options.unit || 'issue';
        const nodes = graph.nodes || [];
        const edges = graph.edges || [];

        const svg = el('svg', {
            viewBox: `0 0 ${WIDTH} ${HEIGHT}`,
            class: 'constellation',
            role: 'group',
            'aria-label': options.label || 'Who appears together'
        });

        const maxCount = nodes.reduce((max, node) => Math.max(max, node.count), 1);
        const maxWeight = edges.reduce((max, edge) => Math.max(max, edge.weight), 1);
        const px = node => 40 + node.x * (WIDTH - 80);
        const py = node => 30 + node.y * (HEIGHT - 60);
        // Area, not radius, in proportion to the count.
        const radius = node => 5 + 17 * Math.sqrt(node.count / maxCount);

        const neighbours = nodes.map(() => new Set());
        const edgeLayer = el('g', { class: 'constellation-edges' });
        const edgeEls = edges.map(edge => {
            const a = nodes[edge.source];
            const b = nodes[edge.target];
            neighbours[edge.source].add(edge.target);
            neighbours[edge.target].add(edge.source);
            const line = el('line', {
                x1: px(a), y1: py(a), x2: px(b), y2: py(b),
                'stroke-width': (0.75 + 2.75 * edge.weight / maxWeight).toFixed(2),
                'stroke-opacity': (0.2 + 0.5 * edge.weight / maxWeight).toFixed(2)
            });
            edgeLayer.appendChild(line);
            return line;
        });
        svg.appendChild(edgeLayer);

        // Where each label goes, decided before anything is drawn.
        const placed = [];      // rectangles already taken by labels
        function overlaps(a, b) {
            return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
        }
        function hitsNode(rect, own) {
            return nodes.some((other, i) => {
                if (i === own) return false;
                const r = radius(other) + 2;
                const nearestX = Math.max(rect.x, Math.min(px(other), rect.x + rect.w));
                const nearestY = Math.max(rect.y, Math.min(py(other), rect.y + rect.h));
                return Math.hypot(px(other) - nearestX, py(other) - nearestY) < r;
            });
        }
        function labelPlace(node, index) {
            const w = node.name.length * LABEL_CHAR_WIDTH + 4;
            const h = LABEL_HEIGHT;
            const r = radius(node);
            const cx = px(node);
            const cy = py(node);
            const options = [
                { anchor: 'start', x: cx + r + LABEL_GAP, y: cy + 4, rect: { x: cx + r + LABEL_GAP, y: cy - h / 2, w, h } },
                { anchor: 'end', x: cx - r - LABEL_GAP, y: cy + 4, rect: { x: cx - r - LABEL_GAP - w, y: cy - h / 2, w, h } },
                { anchor: 'middle', x: cx, y: cy + r + h - 2, rect: { x: cx - w / 2, y: cy + r + 2, w, h } },
                { anchor: 'middle', x: cx, y: cy - r - 6, rect: { x: cx - w / 2, y: cy - r - h - 2, w, h } }
            ];
            const free = options.find(option =>
                option.rect.x >= 0 && option.rect.x + option.rect.w <= WIDTH &&
                option.rect.y >= 0 && option.rect.y + option.rect.h <= HEIGHT &&
                !placed.some(taken => overlaps(option.rect, taken)) &&
                !hitsNode(option.rect, index));
            if (free) placed.push(free.rect);
            // No free place: keep the default side for when the node is lit.
            return { place: free || options[cx > WIDTH * 0.8 ? 1 : 0], shown: Boolean(free) };
        }
        // Nodes arrive most frequent first, so the biggest get first pick.
        const labels = nodes.map(labelPlace);

        const nodeLayer = el('g', { class: 'constellation-nodes' });
        const nodeEls = nodes.map((node, index) => {
            const link = el('a', { class: 'constellation-node', tabindex: '0' });
            const href = options.hrefFor ? options.hrefFor(node) : null;
            if (href) link.setAttribute('href', href);
            if (labels[index].shown) link.classList.add('labelled');

            const count = `${node.count} ${unit}${node.count === 1 ? '' : 's'}`;
            const title = el('title');
            title.textContent = `${node.name}: ${count}`;
            link.appendChild(title);

            link.appendChild(el('circle', { cx: px(node), cy: py(node), r: radius(node).toFixed(1) }));

            const place = labels[index].place;
            const label = el('text', { x: place.x, y: place.y, 'text-anchor': place.anchor });
            label.textContent = node.name;
            link.appendChild(label);

            nodeLayer.appendChild(link);
            return link;
        });
        svg.appendChild(nodeLayer);

        function light(index) {
            svg.classList.add('focused');
            nodeEls.forEach((nodeEl, i) => {
                nodeEl.classList.toggle('lit', i === index || neighbours[index].has(i));
                nodeEl.classList.toggle('current', i === index);
            });
            edgeEls.forEach((edgeEl, i) => {
                edgeEl.classList.toggle('lit', edges[i].source === index || edges[i].target === index);
            });
        }

        function clear() {
            svg.classList.remove('focused');
            nodeEls.forEach(nodeEl => nodeEl.classList.remove('lit', 'current'));
            edgeEls.forEach(edgeEl => edgeEl.classList.remove('lit'));
        }

        nodeEls.forEach((nodeEl, index) => {
            nodeEl.addEventListener('pointerenter', () => light(index));
            nodeEl.addEventListener('pointerleave', clear);
            nodeEl.addEventListener('focus', () => light(index));
            nodeEl.addEventListener('blur', clear);
        });

        container.replaceChildren(svg);
        return svg;
    };
})();
