# three.js r160 (vendored)

Copied unchanged from the npm package `three@0.160.0` (MIT licence, see `LICENSE`), so the
Toolkit's 3D tools load from this site instead of a CDN. Only the files the site imports:

- `three.module.min.js` (`build/three.module.min.js`)
- `addons/controls/OrbitControls.js`
- `addons/environments/RoomEnvironment.js`
- `addons/utils/BufferGeometryUtils.js`

`index.html`'s import map points `three` and `three/addons/` here. To add another add-on,
copy it from `examples/jsm/` in the same three.js version into `addons/`.
