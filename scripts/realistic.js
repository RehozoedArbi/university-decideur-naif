// ============================================================
// realistic.js — Parcours étudiant complet
// Objectif : générer un trafic inter-services réaliste qui active
// toutes les dépendances du graphe OpenTelemetry avec des données
// créées et supprimées à chaque itération (pas de pollution de la DB).
//
// Lancement : ./run-load.sh realistic
// ============================================================

import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Trend, Rate } from 'k6/metrics';
import { STUDENT_URL, TEACHER_URL, ENROLLMENT_URL, TIMEOUT } from './config.js';

const errorsTotal  = new Counter('university_errors_total');
const latencyTrend = new Trend('university_latency_ms');
const successRate  = new Rate('university_success_rate');

export const options = {
  vus:      10,
  duration: '10m',
  thresholds: {
    http_req_failed:         ['rate<0.05'],
    http_req_duration:       ['p(95)<3000'],
    university_success_rate: ['rate>0.95'],
  },
};

function uniqueEmail() {
  return `load_${__VU}_${__ITER}_${Date.now()}@test.com`;
}

function record(r, label) {
  const ok = r.status >= 200 && r.status < 300;
  check(r, { [label]: () => ok });
  successRate.add(ok);
  latencyTrend.add(r.timings.duration);
  if (!ok) errorsTotal.add(1);
  return ok;
}

export default function () {
  const params = {
    headers: { 'Content-Type': 'application/json' },
    timeout: TIMEOUT,
  };

  let studentId, courseId, enrollmentId;

  // ============================================================
  // Groupe 1 — Création (POST)
  // ============================================================
  group('1_setup', () => {
    // Créer un cours (teacher-admin-service)
    let r = http.post(
      `${TEACHER_URL}/courses`,
      JSON.stringify({
        title:       `Cours VU${__VU} iter${__ITER}`,
        description: 'Trafic synthétique',
        capacity:    50,
      }),
      params
    );
    if (!record(r, 'POST /courses 201')) return;
    courseId = r.json('id');
    sleep(0.3);

    // Créer un étudiant (student-service)
    r = http.post(
      `${STUDENT_URL}`,
      JSON.stringify({
        first_name: 'Load',
        last_name:  `VU${__VU}`,
        email:      uniqueEmail(),
      }),
      params
    );
    if (!record(r, 'POST /students 201')) return;
    studentId = r.json('id');
  });

  if (!studentId || !courseId) return;
  sleep(0.5);

  // ============================================================
  // Groupe 2 — Inscription
  // enrollment-service → student-service + teacher-admin-service
  // ============================================================
  group('2_enrollment', () => {
    let r = http.post(
      `${ENROLLMENT_URL}`,
      JSON.stringify({ student_id: studentId, course_id: courseId }),
      params
    );
    if (!record(r, 'POST /enrollments 201')) return;
    enrollmentId = r.json('id');
    sleep(0.3);

    // Vérifier via student-service → enrollment-service (arête inverse)
    r = http.get(`${STUDENT_URL}/${studentId}/enrollments`, params);
    record(r, 'GET /students/{id}/enrollments 200');
  });

  sleep(0.5);

  // ============================================================
  // Groupe 3 — Notes (parcours complet 3 services)
  // ============================================================
  group('3_grades', () => {
    // Ajouter une note (teacher-admin-service)
    let r = http.post(
      `${TEACHER_URL}/grades`,
      JSON.stringify({
        studentId: studentId,
        courseId:  courseId,
        value:     Math.floor(Math.random() * 20),
      }),
      params
    );
    record(r, 'POST /grades 201');
    sleep(0.3);

    // Lire via student-service → enrollment → teacher-admin (3 sauts)
    r = http.get(`${STUDENT_URL}/${studentId}/grades`, params);
    record(r, 'GET /students/{id}/grades 200');
  });

  sleep(0.5);

  // ============================================================
  // Groupe 4 — Nettoyage (évite la croissance infinie de la DB)
  // ============================================================
  group('4_cleanup', () => {
    if (enrollmentId) {
      http.del(`${ENROLLMENT_URL}/${enrollmentId}`, null, params);
    }
    if (studentId) {
      http.del(`${STUDENT_URL}/${studentId}`, null, params);
    }
  });

  sleep(1);
}
