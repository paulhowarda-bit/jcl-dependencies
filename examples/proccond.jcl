//PROCCOND JOB (OPS),'PROC STEP CONDITIONS',CLASS=A
//*
//* A PROC step's condition comes from two artifacts. POSTRUN codes its
//* own COND=(8,LT) inside the PROC; the calling EXEC then applies its own.
//* The calling EXEC's COND overrides the called EXEC's, so each step has
//* the PROC's COND ('cond') and the invocation's ('invokedCond') apart:
//*   NIGHTLY  COND=(4,LT) applies to every step of POSTPROC
//*   WEEKLY   COND.AUDIT=(0,NE) applies to AUDIT alone
//*
//POSTPROC PROC
//POSTRUN  EXEC PGM=POSTRUN,COND=(8,LT)
//LEDGER   DD  DSN=PROD.GL.LEDGER,DISP=SHR
//AUDIT    EXEC PGM=GLAUDIT
//RPT      DD  SYSOUT=*
//         PEND
//*
//NIGHTLY  EXEC POSTPROC,COND=(4,LT)
//WEEKLY   EXEC POSTPROC,COND.AUDIT=(0,NE)
//
