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
 * ties and dims the rest; the largest nodes are always labelled, the others
 * when they are part of what is lit.
 */
(function () {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const WIDTH = 1000;
    const HEIGHT = 560;
    const ALWAYS_LABELLED = 12;

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

        const nodeLayer = el('g', { class: 'constellation-nodes' });
        const nodeEls = nodes.map((node, index) => {
            const link = el('a', { class: 'constellation-node', tabindex: '0' });
            const href = options.hrefFor ? options.hrefFor(node) : null;
            if (href) link.setAttribute('href', href);
            if (index < ALWAYS_LABELLED) link.classList.add('labelled');

            const count = `${node.count} ${unit}${node.count === 1 ? '' : 's'}`;
            const title = el('title');
            title.textContent = `${node.name}: ${count}`;
            link.appendChild(title);

            const r = radius(node);
            link.appendChild(el('circle', { cx: px(node), cy: py(node), r: r.toFixed(1) }));

            // Label to the right, or to the left when it would run off the edge.
            const flip = px(node) > WIDTH * 0.8;
            const label = el('text', {
                x: px(node) + (flip ? -(r + 5) : r + 5),
                y: py(node) + 4,
                'text-anchor': flip ? 'end' : 'start'
            });
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
