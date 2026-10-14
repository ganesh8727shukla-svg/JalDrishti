import { useEffect } from 'react';
import { MapContainer, TileLayer, useMap } from 'react-leaflet';

type Bbox = [number, number, number, number];

function Viewport({ bbox }: { bbox?: Bbox }) {
  const map = useMap();
  useEffect(() => {
    if (!bbox) return;
    map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]], { padding: [24, 24], maxZoom: 12 });
  }, [bbox, map]);
  return null;
}

export default function MapView({ bbox }: { bbox?: Bbox }) {
  return (
    <div className="map-frame">
      <MapContainer
        center={bbox ? [(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2] : [20, 0]}
        zoom={bbox ? 8 : 2}
        minZoom={1}
        scrollWheelZoom
        className="map-canvas"
      >
        <Viewport bbox={bbox} />
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
      </MapContainer>
      <div className="map-label">
        <span className="map-label__eyebrow">MAP VIEW</span>
        <strong>Basemap only</strong>
        <span>Real SAR quicklooks are shown below</span>
      </div>
    </div>
  );
}
