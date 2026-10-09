import { MapContainer, TileLayer } from 'react-leaflet';

export default function MapView() {
  return (
    <div className="map-frame">
      <MapContainer
        center={[28.25, 84.1]}
        zoom={7}
        minZoom={5}
        scrollWheelZoom
        className="map-canvas"
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
      </MapContainer>
      <div className="map-label">
        <span className="map-label__eyebrow">MAP VIEW</span>
        <strong>Basemap only</strong>
        <span>No event observations loaded</span>
      </div>
    </div>
  );
}
