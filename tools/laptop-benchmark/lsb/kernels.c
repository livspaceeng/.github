/*
 * lsburn - native compute kernels for the Livspace laptop benchmark.
 *
 * Deliberately small and dependency-free so it builds with the stock
 * Apple Command Line Tools compiler (or gcc on Linux) in well under a second.
 *
 * Modes:
 *   matmul  dense fp32 matrix multiply      -> GFLOP/s   (CAD solver / meshing proxy)
 *   geom    4x4 transform + normalize       -> Mverts/s  (CAD viewport / tessellation proxy)
 *   stream  STREAM triad over fp64          -> GB/s      (memory bandwidth)
 *
 * Every mode accepts --threads N and either --iters N or --seconds S.
 * Results are printed as one line of JSON on stdout.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <pthread.h>
#include <time.h>
#include <unistd.h>

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

/* ------------------------------------------------------------------ */

typedef struct {
    int      id;
    int      nthreads;
    long     n;          /* matrix dim / vertex count / element count */
    long     iters;      /* 0 => run until deadline */
    double   deadline;   /* absolute monotonic time, 0 => ignore */
    float   *A, *B, *C;  /* matmul */
    float   *V, *M;      /* geom */
    double  *sa, *sb, *sc; /* stream */
    long     completed;
    double   checksum;
} job_t;

/* rows [lo, hi) owned by this thread */
static void slice(long total, int id, int nthreads, long *lo, long *hi) {
    long per = total / nthreads;
    long rem = total % nthreads;
    long start = id * per + (id < rem ? id : rem);
    long len   = per + (id < rem ? 1 : 0);
    *lo = start;
    *hi = start + len;
}

static int keep_going(job_t *j, long done) {
    if (j->deadline > 0.0) return now_s() < j->deadline;
    return done < j->iters;
}

/* ---------------------------- matmul ------------------------------ */

static void *mm_worker(void *p) {
    job_t *j = (job_t *)p;
    long n = j->n, lo, hi, done = 0;
    slice(n, j->id, j->nthreads, &lo, &hi);

    while (keep_going(j, done)) {
        /* i-k-j order: B and C are streamed row-wise, A[i][k] is a scalar. */
        for (long i = lo; i < hi; i++) {
            float *crow = j->C + i * n;
            for (long k = 0; k < n; k++) crow[k] = 0.0f;
            for (long k = 0; k < n; k++) {
                float a = j->A[i * n + k];
                const float *brow = j->B + k * n;
                for (long jj = 0; jj < n; jj++) crow[jj] += a * brow[jj];
            }
        }
        done++;
    }
    j->completed = done;
    /* Consume the result so the optimiser cannot delete the loop. */
    double s = 0.0;
    for (long i = lo; i < hi; i += (hi - lo > 8 ? (hi - lo) / 8 : 1)) s += j->C[i * n + (i % n)];
    j->checksum = s;
    return NULL;
}

/* ----------------------------- geom ------------------------------- */
/* Transform a vertex buffer by a 4x4 matrix and normalise - the inner
 * loop of every CAD viewport redraw and tessellation pass. */

static void *geom_worker(void *p) {
    job_t *j = (job_t *)p;
    long lo, hi, done = 0;
    slice(j->n, j->id, j->nthreads, &lo, &hi);
    const float *M = j->M;
    float acc = 0.0f;

    while (keep_going(j, done)) {
        for (long i = lo; i < hi; i++) {
            float *v = j->V + i * 4;
            float x = v[0], y = v[1], z = v[2], w = v[3];
            float nx = M[0]*x + M[1]*y + M[2]*z  + M[3]*w;
            float ny = M[4]*x + M[5]*y + M[6]*z  + M[7]*w;
            float nz = M[8]*x + M[9]*y + M[10]*z + M[11]*w;
            float nw = M[12]*x + M[13]*y + M[14]*z + M[15]*w;
            float len = sqrtf(nx*nx + ny*ny + nz*nz) + 1e-6f;
            v[0] = nx / len; v[1] = ny / len; v[2] = nz / len; v[3] = nw;
            acc += v[0];
        }
        done++;
    }
    j->completed = done;
    j->checksum = acc;
    return NULL;
}

/* ---------------------------- stream ------------------------------ */

static void *stream_worker(void *p) {
    job_t *j = (job_t *)p;
    long lo, hi, done = 0;
    slice(j->n, j->id, j->nthreads, &lo, &hi);
    const double q = 3.1415926;

    while (keep_going(j, done)) {
        for (long i = lo; i < hi; i++) j->sa[i] = j->sb[i] + q * j->sc[i];
        done++;
    }
    j->completed = done;
    j->checksum = (hi > lo) ? j->sa[lo] : 0.0;
    return NULL;
}

/* ------------------------------------------------------------------ */

static long arg_long(int argc, char **argv, const char *key, long dflt) {
    for (int i = 1; i < argc - 1; i++)
        if (strcmp(argv[i], key) == 0) return atol(argv[i + 1]);
    return dflt;
}
static double arg_double(int argc, char **argv, const char *key, double dflt) {
    for (int i = 1; i < argc - 1; i++)
        if (strcmp(argv[i], key) == 0) return atof(argv[i + 1]);
    return dflt;
}

static void usage(void) {
    fprintf(stderr,
        "usage: lsburn <matmul|geom|stream> [--threads N] [--iters N] [--seconds S]\n"
        "              [--n DIM] [--verts N] [--mib N]\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    const char *mode = argv[1];

    int    nthreads = (int)arg_long(argc, argv, "--threads", 1);
    long   iters    = arg_long(argc, argv, "--iters", 0);
    double seconds  = arg_double(argc, argv, "--seconds", 0.0);
    if (nthreads < 1) nthreads = 1;
    if (iters <= 0 && seconds <= 0.0) iters = 1;

    job_t *jobs = calloc(nthreads, sizeof(job_t));
    pthread_t *th = calloc(nthreads, sizeof(pthread_t));
    void *(*worker)(void *) = NULL;

    long n = 0;
    double bytes_per_iter = 0.0, flops_per_iter = 0.0, items_per_iter = 0.0;
    float *A = NULL, *B = NULL, *C = NULL, *V = NULL;
    double *sa = NULL, *sb = NULL, *sc = NULL;
    static float M[16] = {
        0.9998f, -0.0175f, 0.0f, 1.5f,
        0.0175f,  0.9998f, 0.0f, -2.5f,
        0.0f,     0.0f,    1.0f, 0.75f,
        0.0f,     0.0f,    0.0f, 1.0f
    };

    if (strcmp(mode, "matmul") == 0) {
        n = arg_long(argc, argv, "--n", 512);
        A = malloc((size_t)n * n * sizeof(float));
        B = malloc((size_t)n * n * sizeof(float));
        C = malloc((size_t)n * n * sizeof(float));
        if (!A || !B || !C) { fprintf(stderr, "alloc failed\n"); return 1; }
        for (long i = 0; i < n * n; i++) { A[i] = (float)((i % 97) * 0.01); B[i] = (float)((i % 89) * 0.02); }
        flops_per_iter = 2.0 * (double)n * (double)n * (double)n;
        worker = mm_worker;
    } else if (strcmp(mode, "geom") == 0) {
        n = arg_long(argc, argv, "--verts", 1000000);
        V = malloc((size_t)n * 4 * sizeof(float));
        if (!V) { fprintf(stderr, "alloc failed\n"); return 1; }
        for (long i = 0; i < n * 4; i++) V[i] = (float)((i % 1013) * 0.001 + 0.5);
        items_per_iter = (double)n;
        flops_per_iter = (double)n * 36.0;   /* 28 mul/add + normalise */
        worker = geom_worker;
    } else if (strcmp(mode, "stream") == 0) {
        long mib = arg_long(argc, argv, "--mib", 256);
        n = mib * 1024L * 1024L / (long)sizeof(double) / 3L;
        sa = malloc((size_t)n * sizeof(double));
        sb = malloc((size_t)n * sizeof(double));
        sc = malloc((size_t)n * sizeof(double));
        if (!sa || !sb || !sc) { fprintf(stderr, "alloc failed\n"); return 1; }
        for (long i = 0; i < n; i++) { sa[i] = 0.0; sb[i] = 1.5; sc[i] = 2.5; }
        bytes_per_iter = 3.0 * (double)n * (double)sizeof(double);
        worker = stream_worker;
    } else {
        usage(); return 2;
    }

    double deadline = seconds > 0.0 ? now_s() + seconds : 0.0;

    for (int i = 0; i < nthreads; i++) {
        jobs[i].id = i; jobs[i].nthreads = nthreads; jobs[i].n = n;
        jobs[i].iters = iters; jobs[i].deadline = deadline;
        jobs[i].A = A; jobs[i].B = B; jobs[i].C = C;
        jobs[i].V = V; jobs[i].M = M;
        jobs[i].sa = sa; jobs[i].sb = sb; jobs[i].sc = sc;
    }

    double t0 = now_s();
    for (int i = 1; i < nthreads; i++) pthread_create(&th[i], NULL, worker, &jobs[i]);
    worker(&jobs[0]);
    for (int i = 1; i < nthreads; i++) pthread_join(th[i], NULL);
    double elapsed = now_s() - t0;
    if (elapsed <= 0.0) elapsed = 1e-9;

    /* Threads each own a slice, so one "iter" of work is completed when every
     * thread has finished that iter. Use the minimum to stay honest. */
    long min_done = jobs[0].completed;
    double checksum = 0.0;
    for (int i = 0; i < nthreads; i++) {
        if (jobs[i].completed < min_done) min_done = jobs[i].completed;
        checksum += jobs[i].checksum;
    }

    printf("{\"mode\":\"%s\",\"threads\":%d,\"n\":%ld,\"iters\":%ld,\"seconds\":%.6f",
           mode, nthreads, n, min_done, elapsed);
    if (flops_per_iter > 0.0)
        printf(",\"gflops\":%.4f", flops_per_iter * (double)min_done / elapsed / 1e9);
    if (items_per_iter > 0.0)
        printf(",\"mverts_per_s\":%.4f", items_per_iter * (double)min_done / elapsed / 1e6);
    if (bytes_per_iter > 0.0)
        printf(",\"gb_per_s\":%.4f", bytes_per_iter * (double)min_done / elapsed / 1e9);
    printf(",\"checksum\":%.6g}\n", checksum);
    return 0;
}
