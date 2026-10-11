// ============================================================
// light.js — Charge légère et continue
// Objectif UNIQUE : activer les vraies chaînes d'appels inter-services
// pour alimenter le graphe de dépendances OpenTelemetry.
//
// Chaque requête ici déclenche AU MOINS un appel inter-services :
//   GET /students/{id}/available-courses  → student → teacher-admin
//   GET /students/{id}/enrollments        → student → enrollment
//   GET /students/{id}/grades             → student → enrollment → teacher-admin
//   POST /enrollments                     → enrollment → student + teacher-admin
//
// Pré-requis : données stables en DB (étudiant, cours, inscription)
// créées par ton script de seed.
//
// Lancement : ./run-load.sh light
// ============================================================

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';
import { STUDENT_URL, TEACHER_URL, ENROLLMENT_URL, TIMEOUT } from './config.js';

const errorsTotal  = new Counter('university_errors_total');
const latencyTrend = new Trend('university_latency_ms');

export const options = {
  vus:      2,
  duration: '24h',
  thresholds: {
    http_req_failed:   ['rate<0.05'],
    http_req_duration: ['p(95)<3000'],
  },
};

// IDs stables créés par ton script de seed — à ajuster si nécessaire
// via variable d'environnement : STUDENT_ID=2 COURSE_ID=2 ./run-load.sh light
const STUDENT_ID = parseInt(__ENV.STUDENT_ID  || '1');
const COURSE_ID  = parseInt(__ENV.COURSE_ID   || '1');

export default function () {
  const params = {
    headers: { 'Content-Type': 'application/json' },
    timeout: TIMEOUT,
  };

  let r;

  // ────────────────────────────────────────────────────────
  // Chaîne 1 : student → teacher-admin
  // GET /students/{id}/available-courses
  // ────────────────────────────────────────────────────────
  r = http.get(`${STUDENT_URL}/${STUDENT_ID}/available-courses`, params);
  check(r, { 'student→teacher 200': (res) => res.status === 200 });
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);
  sleep(1);

  // ────────────────────────────────────────────────────────
  // Chaîne 2 : student → enrollment
  // GET /students/{id}/enrollments
  // ────────────────────────────────────────────────────────
  r = http.get(`${STUDENT_URL}/${STUDENT_ID}/enrollments`, params);
  check(r, { 'student→enrollment 200': (res) => res.status === 200 });
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);
  sleep(1);

  // ────────────────────────────────────────────────────────
  // Chaîne 3 : student → enrollment → teacher-admin (3 sauts)
  // GET /students/{id}/grades
  // ────────────────────────────────────────────────────────
  r = http.get(`${STUDENT_URL}/${STUDENT_ID}/grades`, params);
  check(r, { 'student→enrollment→teacher 200': (res) => res.status === 200 });
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);
  sleep(1);

  // ────────────────────────────────────────────────────────
  // Chaîne 4 : enrollment → student + teacher-admin (double dépendance)
  // GET /enrollments?student_id={id}
  // ────────────────────────────────────────────────────────
  r = http.get(`${ENROLLMENT_URL}?student_id=${STUDENT_ID}`, params);
  check(r, { 'enrollment→student+teacher 200': (res) => res.status === 200 });
  latencyTrend.add(r.timings.duration);
  if (r.status !== 200) errorsTotal.add(1);
  sleep(2);
}
