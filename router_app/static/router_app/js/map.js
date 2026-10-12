/* Fuel Route Planner UI — talks only to the /api/route/ endpoint. */
(function () {
  'use strict';

  const map = L.map('map', { zoomControl: true }).setView([39.5, -98.35], 4);
  // Keyless basemaps: standard OpenStreetMap tiles (the classic Leaflet look),
  // with Esri streets as a backup in the layer switcher.
  const osmBase = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  const esriStreets = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors' },
  );
  L.control.layers({ 'OpenStreetMap': osmBase, 'Esri Streets': esriStreets },
    null, { position: 'topright' }).addTo(map);

  const layer = L.layerGroup().addTo(map);
  const stopMarkers = [];

  const $ = (id) => document.getElementById(id);
  const form = $('route-form');
  const button = $('submit-btn');

  const money = (v) => '$' + Number(v).toLocaleString('en-US',
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const miles = (v) => Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 }) + ' mi';
  const hours = (mins) => {
    const h = Math.floor(mins / 60);
    const m = Math.round(mins % 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  };
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  function showError(message, detail) {
    const box = $('error');
    const detailText = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : '';
    box.innerHTML = escapeHtml(message) + (detailText ? `<small>${escapeHtml(detailText)}</small>` : '');
    box.hidden = false;
  }

  function endIcon(kind) {
    return L.divIcon({ className: `end-icon ${kind}`, iconSize: [16, 16] });
  }

  function stopIcon(n) {
    return L.divIcon({ className: 'stop-icon', html: String(n), iconSize: [26, 26] });
  }

  function stopPopup(s, total) {
    return `<b>${escapeHtml(s.station_name)}</b><br>` +
      `${escapeHtml(s.address)}<br>${escapeHtml(s.city)}, ${escapeHtml(s.state)}<br>` +
      `Mile ${s.mile_marker} · ${s.off_route_miles} mi off route<br>` +
      `$${s.price_per_gallon.toFixed(3)}/gal · ${s.gallons_purchased.toFixed(3)} gal<br>` +
      `Cost: <b>${money(s.cost_at_stop)}</b><br>Stop ${s.stop_number} of ${total}`;
  }

  function drawMap(data) {
    layer.clearLayers();
    stopMarkers.length = 0;
    const md = data.map_data;
    const latlngs = md.route_geometry.coordinates.map(([lon, lat]) => [lat, lon]);
    const line = L.polyline(latlngs, { color: '#1d4ed8', weight: 5, opacity: 0.85 }).addTo(layer);

    L.marker([md.start_marker.lat, md.start_marker.lon], { icon: endIcon('start') })
      .bindPopup(`<b>Start</b><br>${escapeHtml(md.start_marker.label)}`).addTo(layer);
    L.marker([md.finish_marker.lat, md.finish_marker.lon], { icon: endIcon('finish') })
      .bindPopup(`<b>Finish</b><br>${escapeHtml(md.finish_marker.label)}`).addTo(layer);

    const stops = data.fuel_plan.stops;
    stops.forEach((s) => {
      const m = L.marker([s.lat, s.lon], { icon: stopIcon(s.stop_number) })
        .bindPopup(stopPopup(s, stops.length)).addTo(layer);
      stopMarkers.push(m);
    });

    if (latlngs.length > 1) map.fitBounds(line.getBounds(), { padding: [30, 30] });
    else map.setView([md.start_marker.lat, md.start_marker.lon], 10);
  }

  function renderSidebar(data) {
    const plan = data.fuel_plan;
    $('s-cost').textContent = money(plan.total_fuel_cost_usd);
    $('s-consumed').textContent = money(plan.fuel_consumed.cost_usd);
    $('s-distance').textContent = miles(data.route.distance_miles);
    $('s-stops').textContent = plan.fuel_stop_count;
    $('s-driving').textContent = hours(data.route.driving_duration_minutes);
    $('s-halting').textContent = hours(data.timing.refueling_halting_minutes);
    $('s-total').textContent = hours(data.timing.total_trip_duration_minutes);

    const rows = Object.entries(data.strategy_comparison).map(([name, c]) => {
      const active = name === data.strategy ? ' class="active"' : '';
      const label = name === 'full_tank' ? 'Full tank' : 'Exact gallon';
      if (c.error) return `<tr${active}><td>${label}</td><td colspan="3">${escapeHtml(c.error)}</td></tr>`;
      return `<tr${active}><td>${label}</td><td>${money(c.total_fuel_cost_usd)}</td>` +
        `<td>${money(c.fuel_consumed_cost_usd)}</td><td>${c.fuel_stop_count}</td></tr>`;
    }).join('');
    $('compare-table').innerHTML =
      '<tr><th>Strategy</th><th>Spent</th><th>Burned</th><th>Stops</th></tr>' + rows;

    const list = $('stops');
    list.innerHTML = '';
    if (!plan.stops.length) {
      list.innerHTML = '<li class="empty">No stops needed — the starting full tank covers this trip.</li>';
    }
    plan.stops.forEach((s, i) => {
      const li = document.createElement('li');
      li.innerHTML =
        `<span class="num">${s.stop_number}</span>` +
        `<span><span class="name">${escapeHtml(s.station_name)}</span><br>` +
        `<span class="meta">${escapeHtml(s.city)}, ${escapeHtml(s.state)} · mile ${s.mile_marker}</span></span>` +
        `<span class="money"><strong>${money(s.cost_at_stop)}</strong>` +
        `<span class="meta">${s.gallons_purchased.toFixed(3)} gal @ $${s.price_per_gallon.toFixed(3)}</span></span>`;
      li.addEventListener('click', () => {
        map.setView(stopMarkers[i].getLatLng(), 11);
        stopMarkers[i].openPopup();
      });
      list.appendChild(li);
    });

    const p = data.performance;
    $('perf').textContent =
      `Algorithm time: ${p.algorithm_computation_ms} ms | Total response: ${p.total_api_response_ms} ms` +
      ` | External API calls: ${p.external_api_calls}` +
      (data.route.served_from_cache ? ' (route cached)' : '');
    $('results').hidden = false;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    $('error').hidden = true;
    button.disabled = true;
    button.textContent = 'Planning…';
    const params = new URLSearchParams(new FormData(form));
    try {
      const response = await fetch(`${window.ROUTE_API_URL}?${params}`);
      const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
      if (!response.ok) {
        showError(data.error || data.detail || `Request failed (HTTP ${response.status})`,
          data.error ? data.detail : '');
        return;
      }
      drawMap(data);
      renderSidebar(data);
    } catch (err) {
      showError('Network error — is the server running?', String(err));
    } finally {
      button.disabled = false;
      button.textContent = 'Plan route';
    }
  });

  // Deep link from the API's `map_url`: pre-fill the form and draw the route.
  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get('start') && urlParams.get('finish')) {
    ['start', 'finish', 'strategy', 'stop_minutes'].forEach((name) => {
      if (urlParams.has(name)) form.elements[name].value = urlParams.get(name);
    });
    form.requestSubmit();
  }
})();
