/* 访客来源地图：高德 JS API 2.0 + DistrictLayer.World 简易行政区图层。
 * - 国家按访客数着色（SOC = ISO alpha-3，后端已下发）；
 * - 悬停显示国家/城市详情；
 * - 放大后出现城市气泡（中国城市坐标后端已转 GCJ-02）；
 * - 右下角南海诸岛小图（同一高德底图，禁交互）；
 * - 左下角图例，底部显示地图来源与审图号。
 */
window.ArxivGeo = (function () {
  var CITY_ZOOM = 3.5; // 缩放级别超过该值时显示城市气泡
  var RANK_LIMIT = 30;

  function isDark() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t === "dark";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function getVisitorColor(v, dark) {
    if (v >= 1000) return "#7f0000";
    if (v >= 500) return "#b30000";
    if (v >= 100) return "#d7301f";
    if (v >= 20) return "#fc8d59";
    if (v > 0) return "#fdcc8a";
    return dark ? "rgba(70, 73, 84, 0.55)" : "#eeeeee";
  }

  function fmt(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function renderRank(items, listId) {
    var list = document.getElementById(listId);
    var top = items.slice(0, RANK_LIMIT);
    var max = top.length ? top[0].visitors : 1;
    var html = "";
    top.forEach(function (c, i) {
      var pct = Math.max(2, Math.round((c.visitors / max) * 100));
      html +=
        '<li class="geo-rank-item">' +
        '<span class="geo-rank-no' + (i < 3 ? " top" : "") + '">' + (i + 1) + "</span>" +
        '<span class="geo-rank-name"></span>' +
        '<span class="geo-rank-bar"><i style="width:' + pct + '%"></i></span>' +
        '<span class="geo-rank-num">' + fmt(c.visitors) + "</span>" +
        "</li>";
    });
    list.innerHTML = html;
    var names = list.querySelectorAll(".geo-rank-name");
    top.forEach(function (c, i) {
      names[i].textContent = c.name;
    });
  }

  function loadAMap(key, securityCode, cb) {
    if (window.AMap) {
      cb();
      return;
    }
    window._AMapSecurityConfig = { securityJsCode: securityCode };
    var s = document.createElement("script");
    s.src =
      "https://webapi.amap.com/maps?v=2.0&key=" +
      encodeURIComponent(key) +
      "&plugin=AMap.DistrictLayer";
    s.onload = cb;
    document.head.appendChild(s);
  }

  function initMap(stats, dark) {
    var countryUV = {};
    var bySoc = {};
    stats.countries.forEach(function (c) {
      if (c.code3) {
        countryUV[c.code3] = c.visitors;
        bySoc[c.code3] = c;
      }
    });

    var mapStyle = dark ? "amap://styles/darkblue" : "amap://styles/whitesmoke";
    var map = new AMap.Map("geo-map", {
      viewMode: "2D",
      zoom: 2.2,
      center: [105, 30],
      showOversea: true,
      mapStyle: mapStyle
    });

    var worldLayer = new AMap.DistrictLayer.World({
      zIndex: 10,
      zooms: [2, 10]
    });
    worldLayer.setStyles({
      "stroke-width": 0.6,
      stroke: dark ? "rgba(255,255,255,0.28)" : "rgba(90,90,90,0.35)",
      fill: function (props) {
        return getVisitorColor(countryUV[props.SOC] || 0, dark);
      }
    });
    map.add(worldLayer);

    // 悬停提示（国家图层 + 城市气泡共用）
    var tip = document.getElementById("geo-map-tip");
    var wrap = document.getElementById("geo-map-wrap");
    function moveTip(ev) {
      if (!ev.pixel) return;
      var x = ev.pixel.x + 14;
      var y = ev.pixel.y + 14;
      if (x + tip.offsetWidth > wrap.clientWidth) x = ev.pixel.x - tip.offsetWidth - 10;
      if (y + tip.offsetHeight > wrap.clientHeight) y = ev.pixel.y - tip.offsetHeight - 10;
      tip.style.left = x + "px";
      tip.style.top = y + "px";
    }
    function setTip(title, lines) {
      tip.innerHTML = "<b></b>" + lines;
      tip.querySelector("b").textContent = title;
      tip.style.display = "block";
    }
    worldLayer.on("mouseover", function (ev) {
      var props = ev.props || {};
      var soc = props.SOC;
      var c = soc ? bySoc[soc] : null;
      var name = (c && c.name) || props.NAME_CHN || props.name || soc || "未知地区";
      var v = c ? c.visitors : 0;
      var share = c && c.share ? (c.share * 100).toFixed(1) + "%" : "0%";
      setTip(name, "<br>访客：" + fmt(v) + "<br>占比：" + share);
      moveTip(ev);
    });
    worldLayer.on("mousemove", moveTip);
    worldLayer.on("mouseout", function () {
      tip.style.display = "none";
    });

    // 城市气泡（半径 ∝ sqrt(访客数)）
    stats.cities.forEach(function (c) {
      var marker = new AMap.CircleMarker({
        center: [c.lng, c.lat],
        radius: Math.min(4 + Math.sqrt(c.visitors) * 1.4, 26),
        strokeColor: dark ? "#ffb4ab" : "#b30000",
        strokeWeight: 1,
        strokeOpacity: 0.9,
        fillColor: dark ? "#f06a5e" : "#d7301f",
        fillOpacity: 0.55,
        zIndex: 120,
        zooms: [CITY_ZOOM, 20],
        cursor: "default",
        extData: c
      });
      marker.on("mouseover", function (ev) {
        setTip(c.name + "，" + c.country, "<br>访客：" + fmt(c.visitors));
        moveTip(ev);
      });
      marker.on("mouseout", function () {
        tip.style.display = "none";
      });
      map.add(marker);
    });

    // 南海诸岛小图（同一高德已审底图，禁交互）
    try {
      new AMap.Map("geo-inset", {
        viewMode: "2D",
        center: [113, 13],
        zoom: 3.8,
        showOversea: true,
        mapStyle: mapStyle,
        dragEnable: false,
        zoomEnable: false,
        doubleClickZoom: false,
        keyboardEnable: false,
        scrollWheel: false,
        touchZoom: false
      });
    } catch (e) {
      /* 小图失败不影响主图 */
    }

    // 审图号
    map.on("complete", function () {
      try {
        var n = map.getMapApprovalNumber && map.getMapApprovalNumber();
        if (n) {
          document.getElementById("geo-map-approval").textContent =
            "地图来源：高德地图　审图号：" + n;
        }
      } catch (e) {
        /* 保持默认文案 */
      }
    });

    // 图例
    var steps = [
      [1, "1+"],
      [20, "20+"],
      [100, "100+"],
      [500, "500+"],
      [1000, "1000+"]
    ];
    var html = "";
    steps.forEach(function (s) {
      html += '<i style="background:' + getVisitorColor(s[0], dark) + '"></i><span>' + s[1] + "</span>";
    });
    document.getElementById("geo-map-legend").innerHTML = html;
  }

  function init(opts) {
    var section = document.getElementById("geo-stats-section");
    if (!section) return;
    var rendered = false;
    var loading = false;
    var reloadRequested = false;

    function loadStats() {
      if (rendered) return;
      if (loading) {
        reloadRequested = true;
        return;
      }
      loading = true;
      fetch(opts.api, { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        return r.json();
      })
      .then(function (stats) {
        if (!stats || !stats.total) return; // 暂无数据则不展示
        if (rendered) return;
        rendered = true;
        section.style.display = "";
        document.getElementById("geo-stats-range").textContent =
          "最近 7 天 · " + stats.since + " ~ " + stats.until;
        document.getElementById("geo-stats-total").textContent =
          fmt(stats.total) + " 位访客 · " + stats.countries.length + " 个国家/地区";
        renderRank(stats.countries, "geo-country-rank-list");
        renderRank(stats.regions || [], "geo-region-rank-list");
        if (opts.key) {
          loadAMap(opts.key, opts.securityCode || "", function () {
            initMap(stats, isDark());
          });
        } else {
          section.classList.add("geo-no-map"); // 未配置高德 key：只显示榜单
        }
      })
      .catch(function () {
        /* 接口失败则保持隐藏 */
      })
      .then(function () {
        loading = false;
        if (reloadRequested && !rendered) {
          reloadRequested = false;
          loadStats();
        }
      });
    }

    // 首位访客打开页面时，初次统计请求可能早于 /api/tab-visit 写入。
    // 写入完成后再拉取一次，避免必须手动刷新才能看到排行榜。
    window.addEventListener("arxiv:tab-visit-recorded", loadStats);
    loadStats();
  }

  return { init: init };
})();
