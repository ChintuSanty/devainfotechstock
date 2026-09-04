import React from 'react';
import { createRoot } from 'react-dom/client';
import { Overlay } from './Overlay';
import '../styles.css';

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Overlay />
  </React.StrictMode>,
);
