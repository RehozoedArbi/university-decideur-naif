// ============================================================
// stress.js — Pic de charge (stress test)
// Objectif : dépasser les seuils du décideur (CPU/latence) pour
// déclencher une proposition d'action de remédiation.
// Profil : montée progressive → maintien → descente
//
// Lancement : ./run-load.sh stress
// ============================================================

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { STUDENT_URL, TEACHER_URL, ENROLLMENT_URL, TIMEOUT } from './config.js';

const errorsTotal  = new Counter('university_errors_total');
const latencyTrend = new Trend('university_latency_ms');

export const options = {
  stages: [
    { duration: '2m', target: 50 },  // montée progressive
    { duration: '5m', target: 50 },  // maintien — décideur doit réagir ici
    { duration: '1m', target: 1  },  // descente
  ],
  thresholds: {
    // Seuils souples — l'objectif est de les dépasser, pas de réussir le test
    http_req_failed:   ['rate<0.30'],
    http_req_duration: ['p(95)<10000'],
  },
};

export default function () {
  const params = {
    headers: { 'Content-Type': 'application/json' },
    timeout: TIMEOUT,
  };

  let r;

  // Appel enrollment direct — le plus coûteux car appelle les 2 autres
  r = http.get(`${ENROLLMENT_URL}`, params);
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);

  // Appel student avec traversée inter-services
  r = http.get(`${STUDENT_URL}/1/available-courses`, params);
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);

  // Appel teacher-admin (JVM, plus lente à absorber les pics)
  r = http.get(`${TEACHER_URL}/courses`, params);
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);

  // Sleep minimal — on veut saturer, pas attendre
  sleep(0.1);
}
